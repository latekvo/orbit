package dev.orbit.protocol

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import java.io.FilterInputStream
import java.io.IOException
import java.io.InputStream
import java.io.OutputStream
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.RejectedExecutionException
import java.util.concurrent.ScheduledExecutorService
import java.util.concurrent.ScheduledFuture
import java.util.concurrent.ThreadPoolExecutor
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
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
    val handshakeMs: Long = 10_000,
    val pingAfterMs: Long = 10_000,
    val idleMs: Long = 30_000,
    val pairingMs: Long = 120_000,
    val tickMs: Long = 1_000,
    /** How long a session may stay unpaired without starting pairing (spec 3.4). */
    val unpairedMs: Long = 30_000,
    /** Outbound messages that may wait for a slow or non-reading peer before the session closes. */
    val maxQueuedSends: Int = 1024,
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
    private val sender = ThreadPoolExecutor(1, 1, 0, TimeUnit.MILLISECONDS, LinkedBlockingQueue(timeouts.maxQueuedSends)) { r ->
        Thread(r, "orbit-send-$id").apply { isDaemon = true }
    }
    private val sendOverflow = AtomicBoolean()
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
    private var establishedAt = 0L
    @Volatile private var lastRecv = startedAt
    @Volatile private var lastSent = startedAt

    /**
     * [stream] whose reads count as receiving (spec 5: any byte counts). Bytes read past a deadline
     * end the session unhandled: they waited in a buffer while this process was frozen (app
     * freezer, doze) and the peer has given up on them and on the session.
     */
    private val inbound = object : ByteStream {
        override val input: InputStream = object : FilterInputStream(stream.input) {
            override fun read(): Int = super.read().also { if (it >= 0) received() }
            override fun read(b: ByteArray, off: Int, len: Int): Int = super.read(b, off, len).also { if (it > 0) received() }
        }
        override val output: OutputStream get() = stream.output
        override fun close() = stream.close()
    }

    private val hubListener = object : ActivityHub.Listener {
        override fun onUpsert(activity: JsonObject) = enqueue { sendActivity(activity) }
        override fun onRemove(id: String) = enqueue { send(buildJsonObject { put("t", "remove"); put("id", id) }) }
    }

    /** Blocks until the session ends. Never throws. */
    fun run() {
        synchronized(lock) {
            if (state == State.CLOSED) return
            ticker = scheduler.scheduleAtFixedRate({ tick() }, timeouts.tickMs, timeouts.tickMs, TimeUnit.MILLISECONDS)
        }
        try {
            val ch = handshake(FrameStream(inbound))
            synchronized(lock) {
                if (state == State.CLOSED) return
                channel = ch
                peer = ch.peer
                sas = ch.sas
                localPinned = host.isPinned(ch.peer)
                establishedAt = clock()
                state = State.UNPAIRED
            }
            host.sessionChanged(this)
            enqueue { send(buildJsonObject { put("t", "ready"); put("paired", localPinned); put("now", clock()) }) }
            while (state != State.CLOSED) {
                handle(ch.recv())
            }
        } catch (e: ProtocolError) {
            close(e.code)
        } catch (e: IOException) {
            close("io: ${e.message}")
        } catch (e: Throwable) {
            // Includes errors such as OutOfMemoryError: a peer must never take the process down.
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
                val wire = activities.map { a -> sendArtFor(a) }
                // Stamp after the art went out: on BLE that can take seconds, and `now` anchors the
                // receiver's clock offset and media positions.
                val now = clock()
                send(buildJsonObject {
                    put("t", "snapshot")
                    put("now", now)
                    put("activities", JsonArray(wire.map { activityForWire(it, now) }))
                })
            }
        }
        // markClosed sets CLOSED before unsubscribing: a close that raced the subscribe is caught here.
        if (state == State.CLOSED) hub.unsubscribe(hubListener)
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

    /** Sends `bye` with [bye] as its reason and closes for [reason]. */
    fun shutdown(reason: String, bye: String = reason) {
        enqueue { send(buildJsonObject { put("t", "bye"); put("reason", bye) }) }
        closeAfterFlush(reason)
    }

    private fun tick() {
        val now = clock()
        if (state == State.CLOSED || expire(now)) return
        if (state != State.HANDSHAKE && now - lastSent >= timeouts.pingAfterMs) {
            lastSent = now // avoid queueing a ping per tick while a slow link drains
            enqueue { send(buildJsonObject { put("t", "ping"); put("req", ++nextPing) }) }
        }
    }

    /** Reader thread: bytes arrived. */
    private fun received() {
        val now = clock()
        if (expire(now)) throw IOException(closeReason)
        lastRecv = now
    }

    /** Closes the session if one of its deadlines has passed at [now]; true if it did. */
    private fun expire(now: Long): Boolean {
        val s = state
        when {
            s == State.CLOSED -> return true
            s == State.HANDSHAKE -> if (now - startedAt >= timeouts.handshakeMs) close("handshake timeout") else return false
            now - lastRecv >= timeouts.idleMs -> close("idle timeout")
            // The peer is still there: tell it why.
            s == State.UNPAIRED && now - establishedAt >= timeouts.unpairedMs -> shutdown("unpaired timeout", bye = "not paired")
            s == State.PAIRING && now - pairingStartedAt >= timeouts.pairingMs -> shutdown("pairing timeout")
            else -> return false
        }
        return true
    }

    // --- sending (sender thread) ---

    private fun enqueue(block: () -> Unit) {
        // Once a message is dropped nothing after it may go out: the peer would see a gap.
        if (sendOverflow.get()) return
        try {
            sender.execute {
                try {
                    block()
                } catch (e: Exception) {
                    close("send failed: ${e.message}")
                }
            }
        } catch (_: RejectedExecutionException) {
            if (sender.isShutdown || !sendOverflow.compareAndSet(false, true)) return
            // Off this thread: hub listeners enqueue while the hub iterates its listeners under its lock.
            try {
                scheduler.execute { close("send queue full") }
            } catch (_: RejectedExecutionException) {
                close("send queue full")
            }
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
        } catch (_: RejectedExecutionException) {
            runCatching { stream.close() }
        }
        sender.shutdown()
        // Bound the flush: a stalled link must not keep the session alive.
        scheduler.schedule({ runCatching { stream.close() } }, 5, TimeUnit.SECONDS)
        host.sessionChanged(this)
    }

    /** Waits until queued sends (such as the `bye` from [shutdown]) are flushed or dropped, after a close. */
    fun awaitClosed(timeoutMs: Long): Boolean = sender.awaitTermination(timeoutMs, TimeUnit.MILLISECONDS)

    fun close(reason: String) {
        if (!markClosed(reason)) return
        sender.shutdownNow()
        runCatching { stream.close() }
        host.sessionChanged(this)
    }

    private fun markClosed(reason: String): Boolean {
        val t: ScheduledFuture<*>?
        synchronized(lock) {
            if (state == State.CLOSED) return false
            state = State.CLOSED
            closeReason = reason
            t = ticker
        }
        t?.cancel(false)
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
