package dev.orbit.protocol

import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import org.junit.Test
import java.io.File
import java.net.InetAddress
import java.net.ServerSocket
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledThreadPoolExecutor

/**
 * Not a unit test: a TCP responder built on the shipping [ResponderSession], for driving with a real
 * orbitd (desktop/tests/lifecycle/test_kotlin_twin.py). Runs only when ORBIT_KPHONE_LOG is set:
 *
 *   ORBIT_KPHONE_LOG=/tmp/k.log ORBIT_KPHONE_SECONDS=120 ./gradlew :protocol:test --tests '*KotlinPhoneRunner*'
 *
 * Auto-accepts pairing; one chat activity whose `notif.0` executions are logged as `ACTION <value>`.
 * The bound port is logged as `LISTENING <port>`; session changes as `SESSION <id> <state> <reason>`.
 */
class KotlinPhoneRunner {
    @Test
    fun run() {
        val logPath = System.getenv("ORBIT_KPHONE_LOG") ?: return
        val seconds = (System.getenv("ORBIT_KPHONE_SECONDS") ?: "120").toLong()
        val log = File(logPath)
        fun line(s: String) = synchronized(log) { log.appendText("${System.currentTimeMillis()} $s\n") }
        val scheduler = ScheduledThreadPoolExecutor(2)
        val threads = Executors.newCachedThreadPool()
        val hub = ActivityHub()
        hub.upsert(json("""{"id":"msg:chat","kind":"generic","app":{"id":"c","name":"Chat"},"title":"Alice",""" +
            """"actions":[{"id":"notif.0","label":"Reply","input":{"kind":"text","label":"Reply"}}],"updatedMs":1}"""))
        val identity = KeyPair.generate()
        val live = ConcurrentHashMap<Long, ResponderSession>()
        val host = object : SessionHost {
            val pins = ConcurrentHashMap<String, Peer>()
            override fun isPinned(peer: Peer) = pins[peer.id]?.static?.contentEquals(peer.static) == true
            override fun pin(peer: Peer) { pins[peer.id] = peer }
            override fun unpin(peerId: String) { pins.remove(peerId) }
            override fun sessionChanged(session: ResponderSession) {
                if (session.state == ResponderSession.State.CLOSED) live.remove(session.id) else live[session.id] = session
                line("SESSION ${session.id} ${session.state} ${session.closeReason ?: ""} live=${live.size}")
                if (session.state == ResponderSession.State.PAIRING && session.localDecision == null) {
                    scheduler.execute { session.decidePairing(true) }
                }
            }
            override fun performAction(activity: JsonObject, actionId: String, value: JsonElement?, done: (Boolean, String?) -> Unit) {
                line("ACTION ${(value as? kotlinx.serialization.json.JsonPrimitive)?.content}")
                done(true, null)
            }
        }
        val server = ServerSocket(0, 50, InetAddress.getLoopbackAddress())
        line("LISTENING ${server.localPort} ${deviceId(identity.public)}")
        Thread {
            while (!server.isClosed) {
                val sock = runCatching { server.accept() }.getOrNull() ?: break
                val s = ResponderSession(SocketByteStream(sock), "lan", identity, "Kotlin Phone", "android", hub, host, scheduler)
                threads.execute { s.run() }
            }
        }.apply { isDaemon = true; start() }
        Thread.sleep(seconds * 1000)
        server.close()
        line("STOPPED")
    }
}
