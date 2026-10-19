package dev.orbit.protocol

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import java.io.IOException
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledExecutorService
import java.util.concurrent.ScheduledFuture
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicLong

/** Everything the responder session needs from the device it runs on. */
interface SessionHost {
    fun isPinned(peer: Peer): Boolean
    fun pin(peer: Peer)
    fun unpin(peerId: String)

    /** Any observable change of [session] (state, pairing decisions, close). */
    fun sessionChanged(session: ResponderSession)

    /**
     * Executes an action of an exported activity. [done] may be called from any thread, exactly once.
     * [value] is the optional `value` field of the `action` message.
     */
    fun performAction(activity: JsonObject, actionId: String, value: JsonElement?, done: (ok: Boolean, error: String?) -> Unit)
}

class SessionTimeouts(
    val handshakeMs: Long = 30_000,
    val pingAfterMs: Long = 20_000,
    val idleMs: Long = 60_000,
    val pairingMs: Long = 120_000,
    val tickMs: Long = 1_000,
)

/** Runs one responder-side session (spec 3-5) over [stream]. Call [run] on a dedicated thread. */
class ResponderSession(
    private val stream: ByteStream,
    val transport: String,
    private val identity: KeyPair,
    private val name: String,
    private val platform: String,
    private val hub: ActivityHub,
    private val host: SessionHost,
    private val scheduler: ScheduledExecutorService,
    private val clock: () -> Long = System::currentTimeMillis,
    private val timeouts: SessionTimeouts = SessionTimeouts(),
    private val handshake: (FrameStream) -> SecureChannel = { Handshake.respond(it, identity, name, platform) },
) {
    enum class State { HANDSHAKE, UNPAIRED, PAIRING, PAIRED, CLOSED }

    val id: Long = nextId.incrementAndGet()

    private val lock = Any()
    private val sender: ExecutorService = Executors.newSingleThreadExecutor { r -> Thread(r, "orbit-send-$id").apply { isDaemon = true } }
    private var channel: SecureChannel? = null
    private var ticker: ScheduledFuture<*>? = null
    private val sentArt = HashSet<String>() // sender thread only
    private var nextPing = 0L

    @Volatile var state: State = State.HANDSHAKE
        private set
    @Volatile var peer: Peer? = null
        private set
    @Volatile var sas: String? = null
        private set
    @Volatile var localDecision: Boolean? = null
        private set
    @Volatile var remoteDecision: Boolean? = null
        private set
    @Volatile var closeReason: String? = null
        private set

    private var localPinned = false
    private var remoteReady: Boolean? = null
    private val startedAt = clock()
    private var pairingStartedAt = 0L
    @Volatile private var lastRecv = startedAt
    @Volatile private var lastSent = startedAt

    private val hubListener = object : ActivityHub.Listener {
        override fun onUpsert(activity: JsonObject) = enqueue { sendActivity(activity) }
        override fun onRemove(id: String) = enqueue { send(buildJsonObject { put("t", "remove"); put("id", id) }) }
    }

    /** Blocks until the session ends. Never throws. */
    fun run() {
        ticker = scheduler.scheduleAtFixedRate({ tick() }, timeouts.tickMs, timeouts.tickMs, TimeUnit.MILLISECONDS)
        try {
            val ch = handshake(FrameStream(stream))
            synchronized(lock) {
                if (state == State.CLOSED) return
                channel = ch
                peer = ch.peer
                sas = ch.sas
                localPinned = host.isPinned(ch.peer)
                state = State.UNPAIRED
            }
            lastRecv = clock()
            host.sessionChanged(this)
            enqueue { send(buildJsonObject { put("t", "ready"); put("paired", localPinned); put("now", clock()) }) }
            while (state != State.CLOSED) {
                val msg = ch.recv()
                lastRecv = clock()
                handle(msg)
            }
        } catch (e: ProtocolError) {
            close(e.code)
        } catch (e: IOException) {
            close("io: ${e.message}")
        } catch (e: RuntimeException) {
            close("error: $e")
        } finally {
            close("ended")
        }
    }

    private fun handle(msg: JsonObject) {
        val t = msg.type
        val paired = state == State.PAIRED
        if (!paired && t !in UNPAIRED_ALLOWED) {
            enqueue { send(buildJsonObject { put("t", "error"); put("code", "not_paired"); put("message", "session is not paired") }) }
            return
        }
        when (t) {
            "ready" -> onReady(msg.bool("paired") == true)
            "pair_request" -> startPairing()
            "pair_decision" -> onRemoteDecision(msg.bool("accepted") == true)
            "ping" -> enqueue { send(buildJsonObject { put("t", "pong"); msg["req"]?.let { put("req", it) } }) }
            "pong", "error" -> {}
            "bye" -> close("peer said bye")
            "unpair" -> {
                peer?.let { host.unpin(it.id) }
                close("unpaired by peer")
            }
            "action" -> onAction(msg)
            else -> {} // Unknown types are ignored for forward compatibility.
        }
    }

    private fun onReady(remotePaired: Boolean) {
        synchronized(lock) {
            if (remoteReady != null || state != State.UNPAIRED) return
            remoteReady = remotePaired
            if (!(remotePaired && localPinned)) return
        }
        becomePaired()
    }

    private fun startPairing() {
        synchronized(lock) {
            if (state != State.UNPAIRED) return
            state = State.PAIRING
            pairingStartedAt = clock()
        }
        host.sessionChanged(this)
    }

    /** The local user's answer to the pairing prompt. */
    fun decidePairing(accepted: Boolean) {
        val complete: Boolean
        synchronized(lock) {
            if (state != State.PAIRING || localDecision != null) return
            localDecision = accepted
            complete = accepted && remoteDecision == true
        }
        enqueue { send(buildJsonObject { put("t", "pair_decision"); put("accepted", accepted) }) }
        when {
            !accepted -> closeAfterFlush("pairing rejected locally")
            complete -> becomePaired()
            else -> host.sessionChanged(this)
        }
    }

    private fun onRemoteDecision(accepted: Boolean) {
        if (!accepted) {
            close("pairing rejected by peer")
            return
        }
        val complete: Boolean
        synchronized(lock) {
            if (state == State.UNPAIRED) {
                // A decision without a prior pair_request still starts the prompt.
                state = State.PAIRING
                pairingStartedAt = clock()
            }
            if (state != State.PAIRING) return
            remoteDecision = true
            complete = localDecision == true
        }
        if (complete) becomePaired() else host.sessionChanged(this)
    }

    private fun becomePaired() {
        val p: Peer
        synchronized(lock) {
            if (state == State.CLOSED || state == State.PAIRED) return
            p = peer ?: return
            if (state == State.PAIRING) {
                host.pin(p)
                localPinned = true
            }
            state = State.PAIRED
        }
        host.sessionChanged(this)
        hub.subscribe(hubListener) { activities ->
            enqueue {
                val now = clock()
                val wire = activities.map { a -> sendArtFor(a) }
                send(buildJsonObject {
                    put("t", "snapshot")
                    put("now", now)
                    put("activities", JsonArray(wire.map { activityForWire(it, now) }))
                })
            }
        }
    }

    private fun onAction(msg: JsonObject) {
        val req = msg["req"]
        fun result(ok: Boolean, error: String?) = enqueue {
            send(buildJsonObject {
                put("t", "action_result")
                put("req", req ?: JsonNull)
                put("ok", ok)
                if (error != null) put("error", error)
            })
        }
        val activityId = msg.str("activity")
        val actionId = msg.str("action")
        if (req == null || activityId == null || actionId == null) return result(false, "bad_request")
        val activity = hub.get(activityId) ?: return result(false, "unknown_activity")
        val listed = (activity["actions"] as? JsonArray)?.any { (it as? JsonObject)?.str("id") == actionId } == true
        // Transport controls stay valid while the listed set flips (play <-> pause, seekable while
        // buffering): the desktop may act on an activity version that is a few ms old.
        val transport = activity["media"] != null && actionId in MEDIA_TRANSPORT
        if (!listed && !transport) return result(false, "unknown_action")
        try {
            host.performAction(activity, actionId, msg["value"]) { ok, error -> result(ok, error) }
        } catch (e: RuntimeException) {
            result(false, e.message ?: e.toString())
        }
    }

    /** Sends `unpair` (when the session is up) and closes; used when the local user forgets the peer. */
    fun unpairAndClose() {
        enqueue { send(buildJsonObject { put("t", "unpair") }) }
        closeAfterFlush("forgotten locally")
    }

    /** Sends `bye` and closes. */
    fun shutdown(reason: String) {
        enqueue { send(buildJsonObject { put("t", "bye"); put("reason", reason) }) }
        closeAfterFlush(reason)
    }

    private fun tick() {
        val now = clock()
        when (state) {
            State.CLOSED -> return
            State.HANDSHAKE -> if (now - startedAt >= timeouts.handshakeMs) close("handshake timeout")
            else -> {
                if (now - lastRecv >= timeouts.idleMs) return close("idle timeout")
                if (state == State.PAIRING && now - pairingStartedAt >= timeouts.pairingMs) return close("pairing timeout")
                if (now - lastSent >= timeouts.pingAfterMs) {
                    lastSent = now // avoid queueing a ping per tick while a slow link drains
                    enqueue { send(buildJsonObject { put("t", "ping"); put("req", ++nextPing) }) }
                }
            }
        }
    }

    // --- sending (sender thread) ---

    private fun enqueue(block: () -> Unit) {
        try {
            sender.execute {
                try {
                    block()
                } catch (e: Exception) {
                    close("send failed: ${e.message}")
                }
            }
        } catch (_: java.util.concurrent.RejectedExecutionException) {
            // Session already closed.
        }
    }

    private fun send(msg: JsonObject) {
        val ch = channel ?: return
        ch.send(msg)
        lastSent = clock()
    }

    private fun sendActivity(activity: JsonObject) {
        val a = sendArtFor(activity)
        val now = clock()
        send(buildJsonObject { put("t", "upsert"); put("now", now); put("activity", activityForWire(a, now)) })
    }

    /** Sends any not-yet-sent art [activity] references; drops references to art no longer available. */
    private fun sendArtFor(activity: JsonObject): JsonObject {
        var a = activity
        for (field in ART_FIELDS) {
            val key = a.str(field) ?: continue
            if (key in sentArt) continue
            val art = hub.art(key)
            if (art == null) {
                a = JsonObject(a - field)
                continue
            }
            send(buildJsonObject { put("t", "art"); put("key", key); put("mime", art.mime); put("data", b64e(art.data)) })
            sentArt += key
        }
        return a
    }

    // --- teardown ---

    private fun closeAfterFlush(reason: String) {
        if (!markClosed(reason)) return
        try {
            sender.execute { runCatching { stream.close() } }
        } catch (_: java.util.concurrent.RejectedExecutionException) {
            runCatching { stream.close() }
        }
        sender.shutdown()
        // Bound the flush: a stalled link must not keep the session alive.
        scheduler.schedule({ runCatching { stream.close() } }, 5, TimeUnit.SECONDS)
        host.sessionChanged(this)
    }

    fun close(reason: String) {
        if (!markClosed(reason)) return
        sender.shutdownNow()
        runCatching { stream.close() }
        host.sessionChanged(this)
    }

    private fun markClosed(reason: String): Boolean {
        synchronized(lock) {
            if (state == State.CLOSED) return false
            state = State.CLOSED
            closeReason = reason
        }
        ticker?.cancel(false)
        hub.unsubscribe(hubListener)
        return true
    }

    companion object {
        private val nextId = AtomicLong()
        private val ART_FIELDS = listOf("iconKey", "artKey")
        private val MEDIA_TRANSPORT = setOf("media.play", "media.pause", "media.next", "media.previous", "media.stop", "media.seek")
        val UNPAIRED_ALLOWED = setOf("ready", "pair_request", "pair_decision", "ping", "pong", "bye", "error")
    }
}
