package dev.orbit.protocol

import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import java.util.concurrent.ScheduledExecutorService
import java.util.concurrent.ScheduledFuture
import java.util.concurrent.TimeUnit
import kotlin.math.abs

/** An image referenced by activities through [key] (spec 5, `art`). */
class Art(val mime: String, val data: ByteArray) {
    /** First 16 hex chars of SHA-256(data). */
    val key: String = sha256(data).toHex().substring(0, 16)
}

/**
 * Media objects may carry `positionAtMs` (epoch ms at which `positionMs` was valid). It never goes
 * on the wire: [ResponderSession] extrapolates `positionMs` to the message's `now` and strips it.
 */
const val POSITION_AT = "positionAtMs"

/**
 * The set of activities currently exported, plus the art they reference. Thread-safe.
 * Listeners are invoked under the hub lock, so they must only enqueue work.
 */
class ActivityHub {
    interface Listener {
        fun onUpsert(activity: JsonObject)
        fun onRemove(id: String)
    }

    private val lock = Any()
    private val activities = LinkedHashMap<String, JsonObject>()
    private val arts = HashMap<String, Art>()
    private val listeners = LinkedHashSet<Listener>()
    private val observers = mutableListOf<(List<JsonObject>) -> Unit>()

    fun upsert(activity: JsonObject, newArts: Collection<Art> = emptyList()) {
        val id = activity.str("id") ?: throw IllegalArgumentException("activity without id")
        val all: List<JsonObject>
        synchronized(lock) {
            newArts.forEach { arts[it.key] = it }
            activities[id] = activity
            gcArts()
            listeners.forEach { it.onUpsert(activity) }
            all = activities.values.toList()
        }
        observers.forEach { it(all) }
    }

    fun remove(id: String) {
        val all: List<JsonObject>
        synchronized(lock) {
            if (activities.remove(id) == null) return
            gcArts()
            listeners.forEach { it.onRemove(id) }
            all = activities.values.toList()
        }
        observers.forEach { it(all) }
    }

    fun get(id: String): JsonObject? = synchronized(lock) { activities[id] }

    fun all(): List<JsonObject> = synchronized(lock) { activities.values.toList() }

    fun art(key: String): Art? = synchronized(lock) { arts[key] }

    /**
     * Registers [listener]; [onSnapshot] receives the current activities under the same lock, so
     * anything it enqueues is ordered before every later listener event.
     */
    fun subscribe(listener: Listener, onSnapshot: (List<JsonObject>) -> Unit) = synchronized(lock) {
        listeners.add(listener)
        onSnapshot(activities.values.toList())
    }

    fun unsubscribe(listener: Listener) = synchronized(lock) { listeners.remove(listener) }

    /** Debug/UI observer, called outside the lock with the full list after every change. */
    fun observe(observer: (List<JsonObject>) -> Unit) {
        synchronized(lock) { observers.add(observer) }
        observer(all())
    }

    private fun gcArts() {
        val used = activities.values.flatMap { listOfNotNull(it.str("iconKey"), it.str("artKey")) }.toSet()
        arts.keys.retainAll(used)
    }
}

/**
 * Debounces activity updates before they reach the [hub] (coalescing bursts of notification
 * updates) and rate-limits updates that only move progress, media position or text.
 */
class ActivityPublisher(
    private val hub: ActivityHub,
    private val scheduler: ScheduledExecutorService,
    private val clock: () -> Long = System::currentTimeMillis,
    private val debounceMs: Long = 150,
    private val churnIntervalMs: Long = 1000,
) {
    private sealed class Pending {
        class Upsert(val activity: JsonObject, val arts: List<Art>) : Pending()
        object Remove : Pending()
    }

    private val lock = Any()
    private val pending = LinkedHashMap<String, Pending>()
    private val lastPublished = HashMap<String, Long>()
    private var flushTask: ScheduledFuture<*>? = null
    private var flushAt = Long.MAX_VALUE

    /** [activity] must not contain `updatedMs`; it is stamped on publish. */
    fun submit(activity: JsonObject, arts: List<Art> = emptyList()) {
        val id = activity.str("id") ?: throw IllegalArgumentException("activity without id")
        synchronized(lock) {
            pending[id] = Pending.Upsert(activity, arts)
            schedule(clock() + debounceMs)
        }
    }

    fun remove(id: String) {
        synchronized(lock) {
            pending[id] = Pending.Remove
            schedule(clock() + debounceMs)
        }
    }

    /** Ids submitted (and not removed) including those still pending. */
    fun knownIds(): Set<String> = synchronized(lock) {
        val ids = hub.all().mapNotNull { it.str("id") }.toMutableSet()
        pending.forEach { (id, p) -> if (p is Pending.Remove) ids.remove(id) else ids.add(id) }
        ids
    }

    private fun schedule(at: Long) {
        if (flushTask != null && flushAt <= at) return
        flushTask?.cancel(false)
        flushAt = at
        flushTask = scheduler.schedule({ flush() }, (at - clock()).coerceAtLeast(0), TimeUnit.MILLISECONDS)
    }

    fun flush() {
        val now = clock()
        val ready = mutableListOf<Pair<String, Pending>>()
        synchronized(lock) {
            flushTask = null
            flushAt = Long.MAX_VALUE
            var next = Long.MAX_VALUE
            val it = pending.entries.iterator()
            while (it.hasNext()) {
                val (id, p) = it.next()
                if (p is Pending.Upsert) {
                    val current = hub.get(id)
                    when (classify(current, p.activity)) {
                        Change.NONE -> { it.remove(); continue }
                        Change.CHURN -> {
                            val due = (lastPublished[id] ?: 0) + churnIntervalMs
                            if (due > now) { next = minOf(next, due); continue }
                        }
                        Change.REAL -> {}
                    }
                    lastPublished[id] = now
                } else {
                    lastPublished.remove(id)
                }
                ready += id to p
                it.remove()
            }
            if (next != Long.MAX_VALUE) schedule(next)
        }
        for ((id, p) in ready) {
            when (p) {
                is Pending.Remove -> hub.remove(id)
                is Pending.Upsert -> hub.upsert(JsonObject(p.activity + ("updatedMs" to JsonPrimitive(now))), p.arts)
            }
        }
    }

    private enum class Change { NONE, CHURN, REAL }

    private fun classify(current: JsonObject?, next: JsonObject): Change {
        if (current == null) return Change.REAL
        val a = JsonObject(current - "updatedMs")
        if (a == next) return Change.NONE
        if (stripChurn(a) != stripChurn(next)) return Change.REAL
        val ma = a["media"] as? JsonObject
        val mb = next["media"] as? JsonObject
        if (ma != null && mb != null && positionConsistent(ma, mb) && a["progress"] == next["progress"] &&
            a["text"] == next["text"] && a["subtitle"] == next["subtitle"]
        ) {
            return Change.NONE
        }
        return Change.CHURN
    }

    /** True when [b]'s position is what extrapolating [a] would predict (no seek happened). */
    private fun positionConsistent(a: JsonObject, b: JsonObject): Boolean {
        val pa = a.long("positionMs") ?: return a["positionMs"] == b["positionMs"]
        val pb = b.long("positionMs") ?: return false
        val ta = a.long(POSITION_AT) ?: return pa == pb
        val tb = b.long(POSITION_AT) ?: return pa == pb
        val rate = if (a.str("state") == "playing") (a["rate"] as? JsonPrimitive)?.content?.toDoubleOrNull() ?: 1.0 else 0.0
        return abs(pa + (tb - ta) * rate - pb) < 1500
    }

    /** Progress notifications usually restate progress in their text ("45% - 68 MB"), so text counts as churn too. */
    private fun stripChurn(o: JsonObject): JsonObject {
        val m = o.toMutableMap()
        m.remove("text")
        m.remove("subtitle")
        (m["progress"] as? JsonObject)?.let { m["progress"] = JsonObject(it - "value") }
        (m["media"] as? JsonObject)?.let { m["media"] = JsonObject(it - "positionMs" - POSITION_AT) }
        return JsonObject(m)
    }
}

/** Returns a copy of [activity] ready for the wire at time [now] (media position extrapolated). */
fun activityForWire(activity: JsonObject, now: Long): JsonObject {
    val media = activity["media"] as? JsonObject ?: return activity
    val at = media.long(POSITION_AT) ?: return activity
    val m = media.toMutableMap<String, JsonElement>()
    m.remove(POSITION_AT)
    val pos = media.long("positionMs")
    if (pos != null && media.str("state") == "playing") {
        val rate = (media["rate"] as? JsonPrimitive)?.content?.toDoubleOrNull() ?: 1.0
        var p = pos + ((now - at) * rate).toLong()
        media.long("durationMs")?.takeIf { it > 0 }?.let { p = p.coerceAtMost(it) }
        m["positionMs"] = JsonPrimitive(p.coerceAtLeast(0))
    }
    return JsonObject(activity + ("media" to JsonObject(m)))
}
