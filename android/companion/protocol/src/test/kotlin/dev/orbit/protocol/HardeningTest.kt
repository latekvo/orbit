package dev.orbit.protocol

import kotlinx.serialization.json.JsonObject
import org.junit.After
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.io.InputStream
import java.io.OutputStream
import java.net.ServerSocket
import java.net.Socket
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.Callable
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledThreadPoolExecutor
import java.util.concurrent.TimeUnit
import kotlin.random.Random

/** Robustness against hostile or broken peers (unauthenticated LAN clients in particular). */
class HardeningTest {
    private val scheduler = ScheduledThreadPoolExecutor(2).apply { removeOnCancelPolicy = true }
    private val threads = Executors.newCachedThreadPool()
    private val server = ServerSocket(0)
    private val phone = KeyPair.generate()
    private val hub = ActivityHub()
    private val opened = mutableListOf<AutoCloseable>()

    @After
    fun tearDown() {
        opened.forEach { runCatching { it.close() } }
        server.close()
        threads.shutdownNow()
        scheduler.shutdownNow()
    }

    private class HookedHost(val fake: FakeHost = FakeHost(), val onChange: (ResponderSession) -> Unit = {}) : SessionHost by fake {
        override fun sessionChanged(session: ResponderSession) = onChange(session)
    }

    private class SlowStream(private val inner: ByteStream) : ByteStream {
        @Volatile var delayMs = 0L
        override val input = inner.input
        override val output = object : OutputStream() {
            override fun write(b: Int) = write(byteArrayOf(b.toByte()), 0, 1)
            override fun write(b: ByteArray, off: Int, len: Int) {
                if (delayMs > 0) Thread.sleep(delayMs)
                inner.output.write(b, off, len)
            }
            override fun flush() = inner.output.flush()
        }
        override fun close() = inner.close()
    }

    /** Accepts one socket and runs a responder session on it; [wrap] adapts the accepted stream. */
    private fun accept(
        host: SessionHost = FakeHost(),
        timeouts: SessionTimeouts = SessionTimeouts(),
        sendBuffer: Int? = null,
        wrap: (ByteStream) -> ByteStream = { it },
    ): ArrayBlockingQueue<ResponderSession> {
        val created = ArrayBlockingQueue<ResponderSession>(1)
        threads.execute {
            val sock = server.accept()
            sendBuffer?.let { sock.sendBufferSize = it }
            val s = ResponderSession(wrap(SocketByteStream(sock)), "lan", phone, "Phone", "android", hub, host, scheduler, timeouts = timeouts)
            created.put(s)
            s.run()
        }
        return created
    }

    private fun dial(receiveBuffer: Int? = null): Socket = Socket().apply {
        receiveBuffer?.let { receiveBufferSize = it }
        soTimeout = 5000
        connect(server.localSocketAddress)
        opened += this
    }

    private fun initiate(sock: Socket, laptop: KeyPair = KeyPair.generate()) =
        Handshake.initiate(FrameStream(SocketByteStream(sock)), laptop, "Laptop", "linux")

    private fun waitFor(what: String, timeoutMs: Long = 5000, cond: () -> Boolean) {
        val deadline = System.currentTimeMillis() + timeoutMs
        while (!cond()) {
            if (System.currentTimeMillis() > deadline) fail("timed out waiting for $what")
            Thread.sleep(10)
        }
    }

    private fun SecureChannel.recvType(t: String): JsonObject {
        while (true) {
            val m = recv()
            if (m.type == t) return m
        }
    }

    private fun hubListeners(): Int =
        (ActivityHub::class.java.getDeclaredField("listeners").apply { isAccessible = true }.get(hub) as Set<*>).size

    private object NullStream : ByteStream {
        override val input: InputStream = ByteArrayInputStream(ByteArray(0))
        override val output: OutputStream = ByteArrayOutputStream()
        override fun close() {}
    }

    // --- run() containment ---

    @Test
    fun runContainsErrorsThrownBySessionCode() {
        val session = ResponderSession(
            NullStream, "lan", phone, "Phone", "android", hub, FakeHost(), scheduler,
            handshake = { throw OutOfMemoryError("frame body") },
        )
        session.run()
        assertEquals(ResponderSession.State.CLOSED, session.state)
        assertTrue(session.closeReason!!, session.closeReason!!.contains("OutOfMemoryError"))
    }

    // --- framing ---

    @Test
    fun handshakeRejectsLargeFrameHeaderWithoutWaitingForTheBody() {
        val created = accept()
        val sock = dial()
        val out = sock.getOutputStream()
        out.write(byteArrayOf(0x00, 0x10, 0x00, 0x00)) // claims a 1 MiB commit, then sends nothing
        out.flush()
        val session = created.take()
        waitFor("closed", timeoutMs = 2000) { session.state == ResponderSession.State.CLOSED }
        assertEquals("bad_frame", session.closeReason)
    }

    @Test
    fun handshakeFrameLimitIs4096Bytes() {
        fun respondTo(length: Int): String {
            val ss = ServerSocket(0).also { opened += it }
            val f = threads.submit(Callable { ss.accept().use { Handshake.respond(FrameStream(SocketByteStream(it)), phone, "Phone", "android") } })
            val sock = Socket("127.0.0.1", ss.localPort).also { opened += it }
            FrameStream(SocketByteStream(sock)).writeFrame(ByteArray(length) { ' '.code.toByte() })
            return (runCatching { f.get(5, TimeUnit.SECONDS) }.exceptionOrNull()?.cause as ProtocolError).code
        }
        assertEquals("bad_message", respondTo(MAX_HANDSHAKE_FRAME))
        assertEquals("bad_frame", respondTo(MAX_HANDSHAKE_FRAME + 1))
    }

    /** Hands out at most [chunk] bytes per read and records the largest buffer it was given. */
    private class RecordingInput(data: ByteArray, private val chunk: Int) : InputStream() {
        private val src = ByteArrayInputStream(data)
        var largestBuffer = 0
        override fun read() = src.read()
        override fun read(b: ByteArray, off: Int, len: Int): Int {
            largestBuffer = maxOf(largestBuffer, b.size)
            return src.read(b, off, minOf(len, chunk))
        }
    }

    private fun frameStreamOver(input: InputStream) = FrameStream(object : ByteStream {
        override val input = input
        override val output: OutputStream = ByteArrayOutputStream()
        override fun close() {}
    })

    @Test
    fun frameBufferTracksBytesReceived() {
        val input = RecordingInput(byteArrayOf(0x00, 0x10, 0x00, 0x00) + ByteArray(10), chunk = 10)
        val e = runCatching { frameStreamOver(input).readFrame() }.exceptionOrNull() as ProtocolError
        assertEquals("closed", e.code)
        assertTrue("allocated ${input.largestBuffer} bytes for 10 received", input.largestBuffer <= 16 * 1024)
    }

    @Test
    fun maxSizeFrameArrivingInSmallChunksIsReassembled() {
        val body = Random(1).nextBytes(MAX_FRAME)
        val input = RecordingInput(byteArrayOf(0x00, 0x10, 0x00, 0x00) + body, chunk = 1000)
        assertArrayEquals(body, frameStreamOver(input).readFrame())
    }

    // --- timeouts and deadlines ---

    @Test
    fun silentConnectionHitsHandshakeTimeout() {
        assertEquals(10_000L, SessionTimeouts().handshakeMs)
        val created = accept(timeouts = SessionTimeouts(handshakeMs = 200, tickMs = 50))
        dial()
        val session = created.take()
        waitFor("closed") { session.state == ResponderSession.State.CLOSED }
        assertEquals("handshake timeout", session.closeReason)
    }

    @Test
    fun unpairedSessionThatNeverPairsIsClosedDespitePings() {
        val created = accept(timeouts = SessionTimeouts(unpairedMs = 400, tickMs = 50))
        val ch = initiate(dial())
        val session = created.take()
        ch.recvType("ready")
        ch.send(json("""{"t":"ready","paired":false,"now":1}"""))
        val deadline = System.currentTimeMillis() + 3000
        while (session.state != ResponderSession.State.CLOSED && System.currentTimeMillis() < deadline) {
            runCatching { ch.send(json("""{"t":"ping","req":1}""")) }
            Thread.sleep(50)
        }
        assertEquals("unpaired timeout", session.closeReason)
    }

    @Test
    fun pairingSessionIsNotSubjectToUnpairedDeadline() {
        val created = accept(timeouts = SessionTimeouts(unpairedMs = 200, tickMs = 50))
        val ch = initiate(dial())
        val session = created.take()
        ch.recvType("ready")
        ch.send(json("""{"t":"pair_request"}"""))
        waitFor("pairing") { session.state == ResponderSession.State.PAIRING }
        Thread.sleep(500)
        assertEquals(ResponderSession.State.PAIRING, session.state)
    }

    // --- outbound queue ---

    @Test
    fun peerThatNeverReadsIsClosedWhenSendQueueFills() {
        val created = accept(timeouts = SessionTimeouts(maxQueuedSends = 64), sendBuffer = 4096)
        val ch = initiate(dial(receiveBuffer = 4096))
        val session = created.take()
        val ping = json("""{"t":"ping","req":1}""")
        val deadline = System.currentTimeMillis() + 10_000
        while (session.state != ResponderSession.State.CLOSED && System.currentTimeMillis() < deadline) {
            runCatching { ch.send(ping) }.onFailure { Thread.sleep(10) }
        }
        assertEquals("send queue full", session.closeReason)
    }

    @Test
    fun hubUpdatesOverflowingTheQueueCloseWithoutBreakingTheHub() {
        val laptop = KeyPair.generate()
        val host = HookedHost()
        host.fake.pin(Peer(deviceId(laptop.public), "Laptop", "linux", laptop.public))
        val created = accept(host, SessionTimeouts(maxQueuedSends = 16), sendBuffer = 4096)
        val ch = initiate(dial(receiveBuffer = 4096), laptop)
        val session = created.take()
        ch.recvType("ready")
        ch.send(json("""{"t":"ready","paired":true,"now":1}"""))
        waitFor("paired") { session.state == ResponderSession.State.PAIRED }
        // A listener after the session's, so a synchronous unsubscribe would break the iteration.
        hub.subscribe(object : ActivityHub.Listener {
            override fun onUpsert(activity: JsonObject) {}
            override fun onRemove(id: String) {}
        }) {}
        val big = "x".repeat(8192)
        var i = 0
        while (session.state != ResponderSession.State.CLOSED && i < 10_000) {
            hub.upsert(json("""{"id":"a$i","kind":"generic","app":{"id":"a","name":"A"},"title":"$big","actions":[],"updatedMs":1}"""))
            hub.remove("a$i")
            i++
        }
        waitFor("closed") { session.state == ResponderSession.State.CLOSED }
        assertEquals("send queue full", session.closeReason)
    }

    // --- teardown ---

    @Test
    fun tickerIsNotScheduledWhenClosedBeforeRun() {
        val session = ResponderSession(NullStream, "lan", phone, "Phone", "android", hub, FakeHost(), scheduler)
        session.close("closed early")
        session.run()
        assertEquals(0, scheduler.queue.size)
    }

    @Test
    fun closeRacingThePairedTransitionLeavesNoHubListener() {
        val laptop = KeyPair.generate()
        // Closes the session between becoming PAIRED and subscribing to the hub.
        val host = HookedHost { s -> if (s.state == ResponderSession.State.PAIRED) s.close("closed while pairing") }
        host.fake.pin(Peer(deviceId(laptop.public), "Laptop", "linux", laptop.public))
        val created = accept(host)
        val ch = initiate(dial(), laptop)
        val session = created.take()
        ch.recvType("ready")
        ch.send(json("""{"t":"ready","paired":true,"now":1}"""))
        waitFor("closed") { session.state == ResponderSession.State.CLOSED }
        Thread.sleep(100) // let becomePaired finish subscribing
        assertEquals("closed while pairing", session.closeReason)
        assertEquals(0, hubListeners())
    }

    @Test
    fun byeIsFlushedBeforeTheLinkIsTornDown() {
        val streams = ArrayBlockingQueue<SlowStream>(1)
        val created = accept(wrap = { s -> SlowStream(s).also { streams.put(it) } })
        val ch = initiate(dial())
        val session = created.take()
        ch.recvType("ready")
        val link = streams.take()
        link.delayMs = 300 // a link that is slow to drain, like BLE
        session.shutdown("service stopped")
        assertTrue(session.awaitClosed(3000))
        link.close() // the transport teardown that follows (stopBle)
        assertEquals("service stopped", ch.recvType("bye").str("reason"))
    }
}
