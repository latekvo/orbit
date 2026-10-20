package dev.orbit.protocol.lan

import dev.orbit.protocol.ActivityHub
import dev.orbit.protocol.KeyPair
import dev.orbit.protocol.Peer
import dev.orbit.protocol.ResponderSession
import dev.orbit.protocol.SessionHost
import dev.orbit.protocol.SocketByteStream
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject
import java.io.File
import java.lang.management.ManagementFactory
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledThreadPoolExecutor
import java.util.concurrent.TimeUnit

/**
 * The real [ResponderSession] behind a plain TCP accept loop (like the app's TcpServer), for LAN
 * fault tests in desktop/tools/lan-faults. Auto-accepts pairing; every action succeeds and is logged.
 *
 *   java -cp <test runtime classpath> dev.orbit.protocol.lan.LanResponderMain PORT STATE_DIR
 *
 * STATE_DIR keeps the identity and pins across restarts and receives events.jsonl and stats.jsonl.
 */
object LanResponderMain {
    @JvmStatic
    fun main(args: Array<String>) {
        val port = args[0].toInt()
        val dir = File(args[1]).apply { mkdirs() }
        val keyFile = File(dir, "key.hex")
        val identity = if (keyFile.exists()) KeyPair.fromScalar(keyFile.readText().trim().unhex())
        else KeyPair.generate().also { keyFile.writeText(it.scalar.hex()) }
        val events = File(dir, "events.jsonl")
        val pinsFile = File(dir, "pins.txt")
        val pins = ConcurrentHashMap<String, ByteArray>()
        if (pinsFile.exists()) pinsFile.readLines().filter { it.isNotBlank() }.forEach {
            val (id, key) = it.split(" "); pins[id] = key.unhex()
        }
        fun event(kind: String, extra: String = "") = synchronized(events) {
            events.appendText("{\"t\": ${System.currentTimeMillis() / 1000.0}, \"kind\": \"$kind\"$extra}\n")
        }

        val scheduler = ScheduledThreadPoolExecutor(2)
        val threads = Executors.newCachedThreadPool()
        val sessions = ConcurrentHashMap<Long, ResponderSession>()
        val hub = ActivityHub()
        var track = 0
        fun media() = Json.parseToJsonElement(
            """{"id":"media:com.example.music","kind":"media","app":{"id":"com.example.music","name":"Music"},
            "title":"Track $track","media":{"state":"playing","positionMs":0,"durationMs":200000,"rate":1.0,"seekable":true},
            "actions":[{"id":"media.next","label":"Next"}],"updatedMs":${System.currentTimeMillis()}}""",
        ).jsonObject

        val host = object : SessionHost {
            override fun isPinned(peer: Peer) = pins[peer.id]?.contentEquals(peer.static) == true
            override fun pin(peer: Peer) {
                pins[peer.id] = peer.static
                pinsFile.writeText(pins.entries.joinToString("\n") { "${it.key} ${it.value.hex()}" })
            }
            override fun unpin(peerId: String) { pins.remove(peerId) }
            override fun sessionChanged(session: ResponderSession) {
                if (session.state == ResponderSession.State.PAIRING && session.localDecision == null) {
                    scheduler.execute { session.decidePairing(true) }
                }
                if (session.state == ResponderSession.State.CLOSED && sessions.remove(session.id) != null) {
                    event("session_closed", ", \"session\": ${session.id}, \"reason\": \"${session.closeReason}\"")
                }
            }
            override fun performAction(activity: JsonObject, actionId: String, value: JsonElement?, done: (Boolean, String?) -> Unit) {
                event("action", ", \"action\": \"$actionId\"")
                if (actionId == "media.next") { track++; hub.upsert(media()) }
                done(true, null)
            }
        }
        hub.upsert(media())
        // Steady traffic like a running download, so the laptop is never idle.
        var pct = 0
        scheduler.scheduleAtFixedRate({
            pct = (pct + 1) % 100
            hub.upsert(Json.parseToJsonElement(
                """{"id":"progress:dl","kind":"progress","app":{"id":"dl","name":"Downloads"},"title":"file.zip",
                "progress":{"value":$pct,"max":100},"actions":[],"updatedMs":${System.currentTimeMillis()}}""",
            ).jsonObject)
        }, 1, 1, TimeUnit.SECONDS)
        val threadsBean = ManagementFactory.getThreadMXBean()
        val stats = File(dir, "stats.jsonl")
        scheduler.scheduleAtFixedRate({
            val fds = File("/proc/self/fd").list()?.size ?: -1
            stats.appendText("{\"t\": ${System.currentTimeMillis() / 1000.0}, \"threads\": ${threadsBean.threadCount}, " +
                "\"sessions\": ${sessions.size}, \"fds\": $fds, \"heap_kb\": ${(Runtime.getRuntime().totalMemory() - Runtime.getRuntime().freeMemory()) / 1024}}\n")
        }, 1, 1, TimeUnit.SECONDS)

        val server = ServerSocket().apply { reuseAddress = true; bind(InetSocketAddress(port)) }
        Runtime.getRuntime().addShutdownHook(Thread {
            sessions.values.forEach { it.shutdown("shutdown") }
            sessions.values.forEach { it.awaitClosed(2000) }
            server.close()
        })
        println("listening on ${server.localPort}, id ${dev.orbit.protocol.deviceId(identity.public)}")
        while (true) {
            val sock = server.accept()
            sock.tcpNoDelay = true
            val s = ResponderSession(SocketByteStream(sock), "lan", identity, "JVM Phone", "android", hub, host, scheduler)
            sessions[s.id] = s
            event("session", ", \"session\": ${s.id}, \"from\": \"${sock.remoteSocketAddress}\"")
            threads.execute { s.run() }
        }
    }
}

private fun String.unhex() = chunked(2).map { it.toInt(16).toByte() }.toByteArray()
private fun ByteArray.hex() = joinToString("") { "%02x".format(it) }
