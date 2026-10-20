package dev.orbit.protocol

import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import org.junit.After
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.OutputStream
import java.net.ServerSocket
import java.net.Socket
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledThreadPoolExecutor
import kotlin.random.Random

/**
 * Responder output paced like BleServer's TX notifications: one [chunk]-byte notification per
 * [intervalMs] (BleServer waits for onNotificationSent before the next one, i.e. about one per
 * connection event). 244 bytes is BleServer's cap (MTU 247+); 15-30 ms is a typical connection
 * interval with a macOS or BlueZ central.
 */
private class NotificationPacedOutput(private val out: OutputStream, private val chunk: Int, private val intervalMs: Long) : OutputStream() {
    override fun write(b: Int) = write(byteArrayOf(b.toByte()), 0, 1)
    override fun write(b: ByteArray, off: Int, len: Int) {
        var pos = off
        while (pos < off + len) {
            val n = minOf(chunk, off + len - pos)
            Thread.sleep(intervalMs)
            out.write(b, pos, n)
            out.flush()
            pos += n
        }
    }
    override fun flush() = out.flush()
}

private class PacedStream(val socket: Socket, chunk: Int, intervalMs: Long) : ByteStream {
    override val input = socket.getInputStream()
    override val output: OutputStream = NotificationPacedOutput(socket.getOutputStream(), chunk, intervalMs)
    override fun close() = socket.close()
}

class BleLinkTimingTest {
    private val scheduler = ScheduledThreadPoolExecutor(2)
    private val threads = Executors.newCachedThreadPool()
    private val server = ServerSocket(0)
    private val phone = KeyPair.generate()
    private val hub = ActivityHub()

    @After
    fun tearDown() {
        server.close()
        threads.shutdownNow()
        scheduler.shutdownNow()
    }

    private fun media(n: Int, art: Art) = json(
        """{"id":"media:player","kind":"media","app":{"id":"p","name":"Player"},"title":"Track $n","artKey":"${art.key}",""" +
            """"media":{"state":"playing"},"actions":[{"id":"media.next","label":"Next"}],"updatedMs":1}""",
    )

    private fun jpeg(size: Int) = Art("image/jpeg", Random.nextBytes(size))

    /** The app's sizer, with variants as large as its BLE budget allows. */
    private val sizer = ArtSizer(ArtTranscoder { art, _, maxBytes -> Art(art.mime, art.data.copyOf(minOf(maxBytes, art.data.size))) })

    private fun session(sock: Socket, chunk: Int, intervalMs: Long, host: SessionHost) =
        ResponderSession(PacedStream(sock, chunk, intervalMs), "ble", phone, "Phone", "android", hub, host, scheduler, artSizer = sizer)

    /** What orbitd sends: it reassembles art parts and answers pings. */
    private fun SecureChannel.ready() = send(json("""{"t":"ready","paired":true,"artParts":true,"now":1}"""))

    private fun SecureChannel.pong(ping: JsonObject) = send(json("""{"t":"pong","req":${ping["req"]}}"""))

    /**
     * The user presses "next" three times, a second apart. Each press makes the player post its
     * notification again with the new track's album art (~[artBytes] JPEG, 256x256 q85), which
     * goes out as its BLE variant while the next click's action_result waits on the same link;
     * orbitd gives up on an action after 10 s.
     */
    private fun nextThreeTimes(chunk: Int, intervalMs: Long, artBytes: Int): List<Long> {
        val laptop = KeyPair.generate()
        val host = object : SessionHost {
            val pins = ConcurrentHashMap<String, Peer>()
            override fun isPinned(peer: Peer) = pins[peer.id]?.static?.contentEquals(peer.static) == true
            override fun pin(peer: Peer) { pins[peer.id] = peer }
            override fun unpin(peerId: String) { pins.remove(peerId) }
            override fun sessionChanged(session: ResponderSession) {}
            var track = 1
            override fun performAction(activity: JsonObject, actionId: String, value: JsonElement?, done: (Boolean, String?) -> Unit) {
                done(true, null)
                // The media app updates its notification a moment later.
                val n = ++track
                scheduler.schedule({ jpeg(artBytes).let { hub.upsert(media(n, it), listOf(it)) } }, 300, java.util.concurrent.TimeUnit.MILLISECONDS)
            }
        }
        host.pins[deviceId(laptop.public)] = Peer(deviceId(laptop.public), "Laptop", "macos", laptop.public)
        jpeg(artBytes).let { hub.upsert(media(1, it), listOf(it)) }
        threads.execute {
            val sock = server.accept()
            session(sock, chunk, intervalMs, host).run()
        }
        val sock = Socket("127.0.0.1", server.localPort)
        val ch = Handshake.initiate(FrameStream(SocketByteStream(sock)), laptop, "Laptop", "macos")
        val results = ConcurrentHashMap<Long, Long>()
        val gotSnapshot = java.util.concurrent.CountDownLatch(1)
        threads.execute {
            runCatching {
                while (true) {
                    val m = ch.recv()
                    when (m.type) {
                        "ping" -> ch.pong(m)
                        "snapshot" -> gotSnapshot.countDown()
                        "action_result" -> results[m.long("req")!!] = System.currentTimeMillis()
                    }
                }
            }
        }
        ch.ready()
        assertTrue("snapshot", gotSnapshot.await(60, java.util.concurrent.TimeUnit.SECONDS))
        val sent = CopyOnWriteArrayList<Long>()
        for (req in 1L..3L) {
            sent += System.currentTimeMillis()
            ch.send(json("""{"t":"action","req":$req,"activity":"media:player","action":"media.next"}"""))
            Thread.sleep(1000)
        }
        val deadline = System.currentTimeMillis() + 60_000
        while (results.size < 3 && System.currentTimeMillis() < deadline) Thread.sleep(20)
        sock.close()
        return (1L..3L).map { req -> results[req]?.let { it - sent[(req - 1).toInt()] } ?: -1 }
    }

    @Test
    fun actionResultsAreNotStuckBehindAlbumArtOnBle() {
        val latencies = nextThreeTimes(chunk = 244, intervalMs = 30, artBytes = 24_000)
        println("action_result latency per click (244-byte notifications every 30 ms, 24 KB art): $latencies ms")
        assertTrue("every click answered within orbitd's 10 s: $latencies", latencies.all { it in 0..10_000 })
        assertTrue("a click is answered within 2 s: $latencies", latencies.all { it in 0..2_000 })
    }

    /**
     * orbitd holds a click made while the device is "reconnecting" and sends it as soon as the new
     * session is up, i.e. right after `ready` - before the phone has sent its snapshot and the art
     * it references (one album art per activity). Returns the action_result latency.
     */
    private fun clickRightAfterReconnect(activities: Int, chunk: Int, intervalMs: Long, artBytes: Int): Long {
        val laptop = KeyPair.generate()
        val applied = CopyOnWriteArrayList<Long>()
        val host = object : SessionHost {
            val pins = ConcurrentHashMap<String, Peer>()
            override fun isPinned(peer: Peer) = pins[peer.id]?.static?.contentEquals(peer.static) == true
            override fun pin(peer: Peer) { pins[peer.id] = peer }
            override fun unpin(peerId: String) { pins.remove(peerId) }
            override fun sessionChanged(session: ResponderSession) {}
            override fun performAction(activity: JsonObject, actionId: String, value: JsonElement?, done: (Boolean, String?) -> Unit) {
                applied += System.currentTimeMillis()
                done(true, null)
            }
        }
        host.pins[deviceId(laptop.public)] = Peer(deviceId(laptop.public), "Laptop", "macos", laptop.public)
        for (i in 1..activities) {
            val art = jpeg(artBytes)
            val id = if (i == 1) "media:player" else "notif:$i"
            hub.upsert(json(media(i, art).toString().replace("media:player", id)), listOf(art))
        }
        threads.execute {
            val sock = server.accept()
            session(sock, chunk, intervalMs, host).run()
        }
        val sock = Socket("127.0.0.1", server.localPort)
        val ch = Handshake.initiate(FrameStream(SocketByteStream(sock)), laptop, "Laptop", "macos")
        var answered = -1L
        val done = java.util.concurrent.CountDownLatch(1)
        threads.execute {
            runCatching {
                while (true) {
                    val m = ch.recv()
                    if (m.type == "ping") ch.pong(m)
                    if (m.type == "action_result") { answered = System.currentTimeMillis(); done.countDown() }
                }
            }
        }
        ch.ready()
        val sent = System.currentTimeMillis()
        ch.send(json("""{"t":"action","req":1,"activity":"media:player","action":"media.next"}"""))
        done.await(90, java.util.concurrent.TimeUnit.SECONDS)
        sock.close()
        println("  applied after ${applied.firstOrNull()?.minus(sent)} ms")
        return if (answered < 0) -1 else answered - sent
    }

    @Test
    fun clickHeldDuringReconnectIsAnsweredWithinOrbitdsDeadline() {
        val latency = clickRightAfterReconnect(activities = 4, chunk = 244, intervalMs = 30, artBytes = 24_000)
        println("action_result latency for a click sent right after reconnect (4 activities with art): $latency ms")
        assertTrue("answered within orbitd's 10 s deadline: $latency ms", latency in 0..10_000)
    }
}
