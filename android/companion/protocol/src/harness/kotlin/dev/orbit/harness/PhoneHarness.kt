package dev.orbit.harness

import dev.orbit.companion.UnauthenticatedSessions
import dev.orbit.protocol.ActivityHub
import dev.orbit.protocol.ActivityPublisher
import dev.orbit.protocol.Art
import dev.orbit.protocol.ByteStream
import dev.orbit.protocol.KeyPair
import dev.orbit.protocol.Peer
import dev.orbit.protocol.ResponderSession
import dev.orbit.protocol.SessionHost
import dev.orbit.protocol.SessionTimeouts
import dev.orbit.protocol.SocketByteStream
import dev.orbit.protocol.b64d
import dev.orbit.protocol.b64e
import dev.orbit.protocol.deviceId
import dev.orbit.protocol.str
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.long
import kotlinx.serialization.json.put
import java.io.File
import java.io.IOException
import java.lang.management.ManagementFactory
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledThreadPoolExecutor
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import kotlin.random.Random
import kotlin.system.exitProcess

/**
 * A JVM stand-in for the Android app's LAN side: the real [ResponderSession] and handshake behind a
 * TCP accept loop shaped like the app's TcpServer/Orbit.startSession, with the app's real
 * [UnauthenticatedSessions] limits (16 total / 4 per address).
 *
 * Commands: one JSON object per stdin line ({"cmd": ...}). Events: one JSON object per stdout line.
 */
class PhoneHarness(private val opts: Map<String, String>) : SessionHost {
    private val state = File(opts["state"] ?: error("--state is required")).apply { mkdirs() }
    val identity: KeyPair = loadIdentity()
    private val name = opts["name"] ?: "JVM Phone"
    private val pinsFile = File(state, "pins.json")
    private val pins = ConcurrentHashMap<String, Peer>().apply { putAll(loadPins()) }
    val hub = ActivityHub()
    val scheduler = ScheduledThreadPoolExecutor(2).apply { removeOnCancelPolicy = true }
    private val publisher = ActivityPublisher(hub, scheduler)
    private val sessionThreads = Executors.newCachedThreadPool { r ->
        Thread(r, "orbit-session").apply { isDaemon = true; setUncaughtExceptionHandler { _, e -> emit("thread_died", "error" to e.toString()) } }
    }
    private val sessions = ConcurrentHashMap<Long, ResponderSession>()
    private val lastState = ConcurrentHashMap<Long, ResponderSession.State>()
    private val unauthenticated = UnauthenticatedSessions(
        opts["max-unauth"]?.toInt() ?: 16, opts["max-unauth-per-address"]?.toInt() ?: 4,
    )
    private val timeouts = SessionTimeouts(
        handshakeMs = opts["handshake-ms"]?.toLong() ?: 10_000,
        pingAfterMs = opts["ping-after-ms"]?.toLong() ?: 10_000,
        idleMs = opts["idle-ms"]?.toLong() ?: 30_000,
        pairingMs = opts["pairing-ms"]?.toLong() ?: 120_000,
        unpairedMs = opts["unpaired-ms"]?.toLong() ?: 30_000,
        maxQueuedSends = opts["max-queued"]?.toInt() ?: 1024,
    )
    @Volatile private var autoAccept = opts["auto-accept"]?.toBoolean() ?: false
    @Volatile private var actionDelayMs = opts["action-delay-ms"]?.toLong() ?: 0
    private val actionCounts = ConcurrentHashMap<String, AtomicInteger>()
    @Volatile private var server: ServerSocket? = null
    private var port = opts["port"]?.toInt() ?: 0
    private val bind = opts["bind"] ?: "127.0.0.1"
    private val accepted = AtomicInteger()
    private val refused = AtomicInteger()

    private fun loadIdentity(): KeyPair {
        val f = File(state, "identity.key")
        if (f.exists()) {
            val b = f.readBytes()
            return KeyPair.fromStored(b.copyOfRange(0, 32), b.copyOfRange(32, 97))
        }
        return KeyPair.generate().also { f.writeBytes(it.scalar + it.public) }
    }

    private fun loadPins(): Map<String, Peer> {
        if (!pinsFile.exists()) return emptyMap()
        return Json.parseToJsonElement(pinsFile.readText()).jsonObject.mapValues { (id, v) ->
            val o = v.jsonObject
            Peer(id, o.str("name")!!, o.str("platform")!!, b64d(o.str("static")!!))
        }
    }

    @Synchronized
    private fun savePins() {
        pinsFile.writeText(
            buildJsonObject {
                pins.forEach { (id, p) -> put(id, buildJsonObject { put("name", p.name); put("platform", p.platform); put("static", b64e(p.static)) }) }
            }.toString(),
        )
    }

    // --- TCP server (mirrors TcpServer + acceptLoop + Orbit.startSession) ---

    fun startServer() {
        val ss = ServerSocket().apply { reuseAddress = true; bind(InetSocketAddress(InetAddress.getByName(bind), port)) }
        port = ss.localPort
        server = ss
        Thread({
            var backoffMs = 0L
            while (!ss.isClosed) {
                val sock = try {
                    ss.accept()
                } catch (e: IOException) {
                    if (ss.isClosed) break
                    emit("accept_failed", "error" to e.toString())
                    backoffMs = (backoffMs * 2).coerceIn(50, 2_000)
                    Thread.sleep(backoffMs)
                    continue
                }
                backoffMs = 0
                try {
                    sock.tcpNoDelay = true
                    accepted.incrementAndGet()
                    startSession(SocketByteStream(sock), "lan", sock.inetAddress.hostAddress, sock.port)
                } catch (e: IOException) {
                    emit("accept_failed", "error" to e.toString())
                    runCatching { sock.close() }
                }
            }
        }, "orbit-tcp-accept").apply { isDaemon = true; start() }
        emit("listening", "port" to port, "deviceId" to deviceId(identity.public), "pid" to ProcessHandle.current().pid())
    }

    fun stopServer() {
        runCatching { server?.close() }
        server = null
        emit("server_closed")
    }

    private fun startSession(stream: ByteStream, transport: String, address: String, remotePort: Int) {
        val session = ResponderSession(stream, transport, identity, name, "android", hub, this, scheduler, timeouts = timeouts)
        if (!unauthenticated.admit(session.id, address)) {
            refused.incrementAndGet()
            emit("refused", "address" to address, "port" to remotePort)
            runCatching { stream.close() }
            return
        }
        sessions[session.id] = session
        emit("session_open", "session" to session.id, "address" to address, "port" to remotePort)
        sessionChanged(session)
        sessionThreads.execute { session.run() }
    }

    // --- SessionHost (mirrors Orbit) ---

    override fun isPinned(peer: Peer) = pins[peer.id]?.static?.contentEquals(peer.static) == true

    override fun pin(peer: Peer) {
        pins[peer.id] = peer
        savePins()
        emit("pinned", "peer" to peer.id, "name" to peer.name)
    }

    override fun unpin(peerId: String) {
        pins.remove(peerId)
        savePins()
        emit("unpinned", "peer" to peerId)
    }

    override fun sessionChanged(session: ResponderSession) {
        val st = session.state
        if (st == ResponderSession.State.CLOSED && sessions.remove(session.id) != null) {
            emit("session_closed", "session" to session.id, "peer" to session.peer?.id, "reason" to session.closeReason)
        }
        if (st == ResponderSession.State.PAIRED || st == ResponderSession.State.CLOSED) unauthenticated.release(session.id)
        if (lastState.put(session.id, st) != st && st != ResponderSession.State.CLOSED) {
            emit("session_state", "session" to session.id, "state" to st.name, "peer" to session.peer?.id, "sas" to session.sas)
        }
        if (st == ResponderSession.State.CLOSED) lastState.remove(session.id)
        if (st == ResponderSession.State.PAIRING && session.localDecision == null && autoAccept) {
            scheduler.execute { session.decidePairing(true) }
        }
    }

    override fun performAction(activity: JsonObject, actionId: String, value: JsonElement?, done: (Boolean, String?) -> Unit) {
        val id = activity.str("id") ?: return done(false, "bad activity")
        val n = actionCounts.computeIfAbsent("$id/$actionId") { AtomicInteger() }.incrementAndGet()
        emit("action", "activity" to id, "action" to actionId, "value" to value?.toString(), "count" to n)
        val delay = actionDelayMs
        if (delay <= 0) done(true, null) else scheduler.schedule({ done(true, null) }, delay, TimeUnit.MILLISECONDS)
    }

    // --- commands ---

    fun command(c: JsonObject) {
        when (c.str("cmd")) {
            "upsert" -> hub.upsert(withUpdated(c["activity"]!!.jsonObject), arts(c))
            "publish" -> publisher.submit(c["activity"]!!.jsonObject, arts(c))
            "remove" -> hub.remove(c.str("id")!!)
            "decide" -> {
                val target = c["session"]?.jsonPrimitive?.long
                sessions.values.filter { target == null || it.id == target }
                    .filter { it.state == ResponderSession.State.PAIRING && it.localDecision == null }
                    .forEach { it.decidePairing(c["accept"]!!.jsonPrimitive.boolean) }
            }
            "auto_accept" -> autoAccept = c["value"]!!.jsonPrimitive.boolean
            "action_delay" -> actionDelayMs = c["ms"]!!.jsonPrimitive.long
            "art_burst" -> artBurst(c)
            "churn" -> churn(c)
            "shutdown" -> sessions.values.forEach { it.shutdown(c.str("reason") ?: "harness shutdown") }
            "close_sessions" -> sessions.values.forEach { it.close(c.str("reason") ?: "harness close") }
            "stop_server" -> stopServer()
            "start_server" -> startServer()
            "stats" -> stats(c["gc"]?.jsonPrimitive?.boolean ?: true)
            "exit" -> { emit("exiting"); System.out.flush(); exitProcess(0) }
            else -> emit("bad_command", "command" to c.toString())
        }
    }

    private fun withUpdated(a: JsonObject) =
        if (a.containsKey("updatedMs")) a else JsonObject(a + ("updatedMs" to JsonPrimitive(System.currentTimeMillis())))

    private fun arts(c: JsonObject): List<Art> =
        c["arts"]?.jsonArray?.map { val o = it.jsonObject; Art(o.str("mime")!!, b64d(o.str("b64")!!)) } ?: emptyList()

    /** Upserts [count] versions of one media activity, each with fresh random album art of [bytes] bytes. */
    private fun artBurst(c: JsonObject) {
        val count = c["count"]?.jsonPrimitive?.int ?: 10
        val bytes = c["bytes"]?.jsonPrimitive?.int ?: 60_000
        val id = c.str("activity") ?: "media:burst"
        repeat(count) { i ->
            val art = Art("image/jpeg", Random.nextBytes(bytes))
            hub.upsert(
                withUpdated(
                    buildJsonObject {
                        put("id", id); put("kind", "media")
                        put("app", buildJsonObject { put("id", "com.burst"); put("name", "Burst") })
                        put("title", "Track $i"); put("artKey", art.key)
                        put("actions", buildJsonArray { add(buildJsonObject { put("id", "media.pause"); put("label", "Pause") }) })
                    },
                ),
                listOf(art),
            )
        }
        emit("art_burst_done", "count" to count, "bytes" to bytes)
    }

    /** Upserts [count] changes of one progress activity back to back (no debounce), like a chatty download. */
    private fun churn(c: JsonObject) {
        val count = c["count"]?.jsonPrimitive?.int ?: 1000
        val id = c.str("activity") ?: "progress:churn"
        val perSecond = c["rate"]?.jsonPrimitive?.int ?: 0
        if (perSecond > 0) {
            Thread({ repeat(count) { i -> churnOne(id, i, count); Thread.sleep(1000L / perSecond) }; emit("churn_done", "count" to count) }, "churn").start()
            return
        }
        repeat(count) { i -> churnOne(id, i, count) }
        emit("churn_done", "count" to count)
    }

    private fun churnOne(id: String, i: Int, count: Int) {
        run {
            hub.upsert(
                withUpdated(
                    buildJsonObject {
                        put("id", id); put("kind", "progress")
                        put("app", buildJsonObject { put("id", "com.churn"); put("name", "Churn") })
                        put("title", "Download"); put("text", "step $i")
                        put("progress", buildJsonObject { put("value", i); put("max", count) })
                        put("actions", buildJsonArray {})
                    },
                ),
            )
        }
    }

    private fun stats(gc: Boolean) {
        if (gc) repeat(2) { System.gc(); Thread.sleep(50) }
        val threads = Thread.getAllStackTraces().keys
        val byName = threads.groupingBy { it.name.replace(Regex("-\\d+$"), "-N") }.eachCount()
        val fdDir = listOf(File("/proc/self/fd"), File("/dev/fd")).firstOrNull { it.isDirectory }
        val mem = ManagementFactory.getMemoryMXBean().heapMemoryUsage
        val held = UnauthenticatedSessions::class.java.getDeclaredField("held").apply { isAccessible = true }
            .get(unauthenticated) as Map<*, *>
        emit(
            "stats",
            "threads" to ManagementFactory.getThreadMXBean().threadCount,
            "threadNames" to counts(byName),
            "fds" to (fdDir?.list()?.size ?: -1),
            "heapUsed" to mem.used,
            "sessions" to sessions.size,
            "sessionStates" to counts(sessions.values.groupingBy { it.state.name }.eachCount()),
            "unauthenticated" to synchronized(unauthenticated) { held.size },
            "schedulerQueue" to scheduler.queue.size,
            "accepted" to accepted.get(),
            "refused" to refused.get(),
            "actions" to counts(actionCounts.mapValues { it.value.get() }),
        )
    }

    companion object {
        private val out = Any()

        fun emit(ev: String, vararg fields: Pair<String, Any?>) {
            val line = buildJsonObject {
                put("ev", ev)
                put("t", System.currentTimeMillis())
                for ((k, v) in fields) {
                    when (v) {
                        null -> put(k, JsonNull)
                        is Number -> put(k, v)
                        is Boolean -> put(k, v)
                        is JsonElement -> put(k, v)
                        else -> put(k, v.toString())
                    }
                }
            }.toString()
            synchronized(out) { println(line); System.out.flush() }
        }
    }
}

fun main(args: Array<String>) {
    val opts = HashMap<String, String>()
    var i = 0
    while (i < args.size) {
        val k = args[i].removePrefix("--")
        val v = args.getOrNull(i + 1)
        if (v == null || v.startsWith("--")) { opts[k] = "true"; i += 1 } else { opts[k] = v; i += 2 }
    }
    val h = PhoneHarness(opts)
    h.startServer()
    val reader = System.`in`.bufferedReader()
    while (true) {
        val line = reader.readLine() ?: break
        if (line.isBlank()) continue
        try {
            h.command(Json.parseToJsonElement(line).jsonObject)
        } catch (e: Exception) {
            PhoneHarness.emit("command_failed", "line" to line, "error" to e.toString())
        }
    }
    PhoneHarness.emit("stdin_closed")
    exitProcess(0)
}

private fun counts(m: Map<String, Int>): JsonObject = buildJsonObject { m.forEach { (k, v) -> put(k, v) } }
