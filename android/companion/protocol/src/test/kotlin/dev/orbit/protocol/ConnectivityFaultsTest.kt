package dev.orbit.protocol

import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.net.ServerSocket
import java.net.Socket
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledThreadPoolExecutor
import java.util.concurrent.atomic.AtomicLong

/** Link faults seen from the responder: a frozen process, a frame still trickling in, a non-reading peer. */
class ConnectivityFaultsTest {
    private val scheduler = ScheduledThreadPoolExecutor(2)
    private val threads = Executors.newCachedThreadPool()
    private val server = ServerSocket(0)
    private val phone = KeyPair.generate()
    private val laptop = KeyPair.generate()
    private val host = FakeHost()
    private val hub = ActivityHub()
    private val now = AtomicLong(1_760_000_000_000)
    private val sockets = mutableListOf<Socket>()

    @After
    fun tearDown() {
        sockets.forEach { runCatching { it.close() } }
        server.close()
        threads.shutdownNow()
        scheduler.shutdownNow()
    }

    /** Lets a test take the bytes of one encrypted record instead of writing it. */
    private class CapturingFrames(stream: ByteStream) : FrameStream(stream) {
        @Volatile var capture = false
        @Volatile var captured: ByteArray? = null
        override fun writeFrame(body: ByteArray) {
            if (capture) captured = body else super.writeFrame(body)
        }
    }

    private class Client(val ch: SecureChannel, val frames: CapturingFrames, val socket: Socket) {
        fun recvType(t: String): JsonObject {
            while (true) {
                val m = ch.recv()
                if (m.type == t) return m
                if (m.type != "ping" && m.type != "art" && m.type != "upsert") fail("expected $t, got $m")
            }
        }
    }

    private fun pairedSession(timeouts: SessionTimeouts): Pair<ResponderSession, Client> {
        host.pins[deviceId(laptop.public)] = Peer(deviceId(laptop.public), "Laptop", "linux", laptop.public)
        val created = ArrayBlockingQueue<ResponderSession>(1)
        threads.execute {
            val sock = server.accept()
            val s = ResponderSession(SocketByteStream(sock), "lan", phone, "Phone", "android", hub, host, scheduler, clock = now::get, timeouts = timeouts)
            created.put(s)
            s.run()
        }
        val sock = Socket("127.0.0.1", server.localPort).apply { soTimeout = 5000 }
        sockets += sock
        val frames = CapturingFrames(SocketByteStream(sock))
        val ch = Handshake.initiate(frames, laptop, "Laptop", "linux")
        val c = Client(ch, frames, sock)
        c.recvType("ready")
        ch.send(json("""{"t":"ready","paired":true,"now":1}"""))
        c.recvType("snapshot")
        val session = created.take()
        assertEquals(ResponderSession.State.PAIRED, session.state)
        return session to c
    }

    private fun waitFor(what: String, ms: Long = 5000, cond: () -> Boolean) {
        val deadline = System.currentTimeMillis() + ms
        while (!cond()) {
            if (System.currentTimeMillis() > deadline) fail("timed out waiting for $what")
            Thread.sleep(10)
        }
    }

    private val media = json(
        """{"id":"media:x","kind":"media","app":{"id":"x","name":"X"},"title":"T","media":{"state":"playing"},""" +
            """"actions":[{"id":"media.pause","label":"Pause"}],"updatedMs":1}""",
    )

    /**
     * The process was frozen (cached-app freezer, OEM battery saver) past the idle deadline while an
     * action sat in the socket buffer. The initiator has long given up on it (and on the session);
     * on thaw the reader runs before the ticker, and must not execute the abandoned action.
     */
    @Test
    fun actionReadAfterTheIdleDeadlineIsNotExecuted() {
        hub.upsert(media)
        // Ticks never fire during the test: the deadline passes while the process is "frozen".
        val (session, c) = pairedSession(SessionTimeouts(tickMs = 3_600_000))
        now.addAndGet(45_000)
        c.ch.send(json("""{"t":"action","req":7,"activity":"media:x","action":"media.pause"}"""))
        Thread.sleep(500)
        assertTrue("executed an action read 45 s after the last byte: ${host.actions}", host.actions.isEmpty())
        assertEquals(ResponderSession.State.CLOSED, session.state)
    }

    /** spec 5: "any byte counts, so a large frame still arriving keeps the session alive". */
    @Test
    fun bytesOfAFrameStillArrivingKeepTheSessionAlive() {
        val (session, c) = pairedSession(SessionTimeouts(tickMs = 20, pingAfterMs = 3_600_000))
        c.frames.capture = true
        c.ch.send(json("""{"t":"ping","req":1,"pad":"${"x".repeat(200)}"}"""))
        c.frames.capture = false
        val body = c.frames.captured!!
        val frame = java.nio.ByteBuffer.allocate(4 + body.size).putInt(body.size).put(body).array()
        val out = c.socket.getOutputStream()
        val parts = listOf(0 until 60, 60 until 120, 120 until frame.size)
        for ((i, r) in parts.withIndex()) {
            out.write(frame.sliceArray(r))
            out.flush()
            Thread.sleep(150) // the reader takes the bytes
            if (i < parts.lastIndex) {
                now.addAndGet(20_000) // 20 s pass between chunks: under idleMs each time
                Thread.sleep(150) // ticks run
                assertNotEquals("closed while a frame was still arriving (${session.closeReason})", ResponderSession.State.CLOSED, session.state)
            }
        }
        assertEquals("pong", c.recvType("pong").type)
    }

    /** The peer stops reading: the bounded queue overflows, the session closes and its sender thread ends. */
    @Test
    fun nonReadingPeerOverflowsTheQueueAndCleansUp() {
        val (session, _) = pairedSession(SessionTimeouts(maxQueuedSends = 8))
        val before = Thread.getAllStackTraces().keys.count { it.name == "orbit-send-${session.id}" }
        assertEquals(1, before)
        repeat(5_000) { i -> hub.upsert(JsonObject(media + ("title" to JsonPrimitive("T$i")))) }
        waitFor("close") { session.state == ResponderSession.State.CLOSED }
        assertEquals("send queue full", session.closeReason)
        assertTrue("sender thread still running", session.awaitClosed(5_000))
        waitFor("sender thread gone") { Thread.getAllStackTraces().keys.none { it.name == "orbit-send-${session.id}" } }
    }
}
