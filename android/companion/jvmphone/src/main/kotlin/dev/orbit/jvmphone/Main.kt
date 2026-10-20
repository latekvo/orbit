package dev.orbit.jvmphone

import dev.orbit.protocol.ActivityHub
import dev.orbit.protocol.KeyPair
import dev.orbit.protocol.Peer
import dev.orbit.protocol.ResponderSession
import dev.orbit.protocol.SessionHost
import dev.orbit.protocol.SocketByteStream
import dev.orbit.protocol.b64d
import dev.orbit.protocol.b64e
import dev.orbit.protocol.deviceId
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import java.io.File
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledThreadPoolExecutor
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

/**
 * Headless stand-in for the companion app: the real [ResponderSession] over real TCP, with the
 * same wiring as Orbit.kt/TcpServer.kt (shared hub, scheduler, cached session threads). Pairing is
 * auto-accepted. One activity "counter" whose action `notif.0` increments a counter; every
 * execution is logged as `EXEC n=<count>` so duplicate or late execution is visible.
 *
 * Usage: jvmphone <stateDir> <port> [actionDelayMs]
 */
fun main(args: Array<String>) {
    val state = File(args[0]).apply { mkdirs() }
    val port = args[1].toInt()
    val actionDelayMs = args.getOrNull(2)?.toLong() ?: 0L
    val idFile = File(state, "identity.key")
    val identity = if (idFile.exists()) {
        val b = idFile.readBytes(); KeyPair.fromStored(b.copyOfRange(0, 32), b.copyOfRange(32, 97))
    } else {
        KeyPair.generate().also { idFile.writeBytes(it.scalar + it.public) }
    }
    val pinsFile = File(state, "pins.json")
    val pins = ConcurrentHashMap<String, String>()
    if (pinsFile.exists()) Json.parseToJsonElement(pinsFile.readText()).jsonObject.forEach { (k, v) -> pins[k] = v.jsonPrimitive.content }
    fun savePins() = pinsFile.writeText(JsonObject(pins.mapValues { kotlinx.serialization.json.JsonPrimitive(it.value) }).toString())

    val hub = ActivityHub()
    val scheduler = ScheduledThreadPoolExecutor(2).apply { removeOnCancelPolicy = true }
    val threads = Executors.newCachedThreadPool { r -> Thread(r, "orbit-session").apply { isDaemon = true } }
    val count = AtomicInteger()
    val sessions = ConcurrentHashMap<Long, ResponderSession>()

    fun publish() {
        hub.upsert(buildJsonObject {
            put("id", "counter")
            put("kind", "generic")
            put("app", buildJsonObject { put("id", "dev.orbit.jvmphone"); put("name", "JvmPhone") })
            put("title", "count ${count.get()}")
            put("actions", buildJsonArray {
                add(buildJsonObject { put("id", "notif.0"); put("label", "Increment") })
                add(buildJsonObject { put("id", "notif.1"); put("label", "Art burst") })
            })
            put("updatedMs", System.currentTimeMillis())
        })
    }
    publish()

    val host = object : SessionHost {
        override fun isPinned(peer: Peer) = pins[peer.id] == b64e(peer.static)
        override fun pin(peer: Peer) { pins[peer.id] = b64e(peer.static); savePins(); log("paired with ${peer.name} (${peer.id})") }
        override fun unpin(peerId: String) { pins.remove(peerId); savePins() }
        override fun sessionChanged(session: ResponderSession) {
            log("session ${session.id} state=${session.state}" + (session.closeReason?.let { " reason=$it" } ?: ""))
            if (session.state == ResponderSession.State.CLOSED) sessions.remove(session.id)
            if (session.state == ResponderSession.State.PAIRING && session.localDecision == null) {
                scheduler.execute { session.decidePairing(true) }
            }
        }
        override fun performAction(activity: JsonObject, actionId: String, value: JsonElement?, done: (Boolean, String?) -> Unit) {
            if (actionId == "notif.1") {
                // Like a media app posting new album art: ART_BURST activities, each with fresh ~30 KB art.
                // Answered first: the burst stands for notifications posted independently of any action.
                done(true, null)
                val n = System.getenv("ART_BURST")?.toInt() ?: 4
                val rnd = java.util.Random()
                Thread.sleep(200)
                repeat(n) { i ->
                    val art = dev.orbit.protocol.Art("image/jpeg", ByteArray(30_000).also { rnd.nextBytes(it) })
                    hub.upsert(buildJsonObject {
                        put("id", "media:$i"); put("kind", "media"); put("title", "track ${System.nanoTime()}")
                        put("app", buildJsonObject { put("id", "app$i"); put("name", "App $i") })
                        put("artKey", art.key); put("actions", JsonArray(emptyList())); put("updatedMs", System.currentTimeMillis())
                    }, listOf(art))
                }
                log("ART burst n=$n")
                return
            }
            scheduler.schedule({
                val n = count.incrementAndGet()
                log("EXEC action=$actionId n=$n")
                publish()
                done(true, null)
            }, actionDelayMs, TimeUnit.MILLISECONDS)
        }
    }

    val ss = ServerSocket().apply { reuseAddress = true; bind(InetSocketAddress(port)) }
    log("listening port=${ss.localPort} id=${deviceId(identity.public)} pid=${ProcessHandle.current().pid()}")
    Runtime.getRuntime().addShutdownHook(Thread { log("shutdown hook: bye to ${sessions.size}"); sessions.values.forEach { it.shutdown("service stopped") }; Thread.sleep(300) })
    while (true) {
        val sock = ss.accept()
        sock.tcpNoDelay = true
        val s = ResponderSession(SocketByteStream(sock), "lan", identity, "JvmPhone", "android", hub, host, scheduler)
        sessions[s.id] = s
        log("session ${s.id} open from ${sock.remoteSocketAddress}")
        threads.execute { s.run() }
    }
}

private val start = System.currentTimeMillis()
fun log(msg: String) {
    println("%d %s".format(System.currentTimeMillis(), msg))
    System.out.flush()
}
