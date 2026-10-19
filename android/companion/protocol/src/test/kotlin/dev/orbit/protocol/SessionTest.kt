package dev.orbit.protocol

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.net.ServerSocket
import java.net.Socket
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledThreadPoolExecutor

fun json(text: String): JsonObject = Json.parseToJsonElement(text).jsonObject

class FakeHost : SessionHost {
    val pins = ConcurrentHashMap<String, Peer>()
    val actions = CopyOnWriteArrayList<Triple<String, String, JsonElement?>>()
    var actionOutcome: Pair<Boolean, String?> = true to null

    override fun isPinned(peer: Peer) = pins[peer.id]?.static?.contentEquals(peer.static) == true
    override fun pin(peer: Peer) { pins[peer.id] = peer }
    override fun unpin(peerId: String) { pins.remove(peerId) }
    override fun sessionChanged(session: ResponderSession) {}
    override fun performAction(activity: JsonObject, actionId: String, value: JsonElement?, done: (Boolean, String?) -> Unit) {
        actions += Triple(activity.str("id")!!, actionId, value)
        done(actionOutcome.first, actionOutcome.second)
    }
}

class SessionTest {
    private val scheduler = ScheduledThreadPoolExecutor(2)
    private val threads = Executors.newCachedThreadPool()
    private val server = ServerSocket(0)
    private val phone = KeyPair.generate()
    private val host = FakeHost()
    private val hub = ActivityHub()
    private val opened = mutableListOf<AutoCloseable>()

    @After
    fun tearDown() {
        opened.forEach { runCatching { it.close() } }
        server.close()
        threads.shutdownNow()
        scheduler.shutdownNow()
    }

    private class Client(val ch: SecureChannel, val socket: Socket) : AutoCloseable {
        fun send(text: String) = ch.send(json(text))
        fun recv(): JsonObject = ch.recv()
        fun recvType(t: String): JsonObject {
            while (true) {
                val m = recv()
                if (m.type == t) return m
                if (m.type != "ping") fail("expected $t, got $m")
            }
        }
        override fun close() = socket.close()
    }

    /** Starts a responder session on the next accepted socket and connects an initiator to it. */
    private fun connect(laptop: KeyPair = KeyPair.generate(), timeouts: SessionTimeouts = SessionTimeouts()): Pair<ResponderSession, Client> {
        val created = java.util.concurrent.ArrayBlockingQueue<ResponderSession>(1)
        threads.execute {
            val sock = server.accept()
            val s = ResponderSession(SocketByteStream(sock), "lan", phone, "Phone", "android", hub, host, scheduler, timeouts = timeouts)
            created.put(s)
            s.run()
        }
        val sock = Socket("127.0.0.1", server.localPort).apply { soTimeout = 5000 }
        val ch = Handshake.initiate(FrameStream(SocketByteStream(sock)), laptop, "Laptop", "linux")
        return created.take() to Client(ch, sock).also { opened += it }
    }

    private fun waitFor(what: String, cond: () -> Boolean) {
        val deadline = System.currentTimeMillis() + 5000
        while (!cond()) {
            if (System.currentTimeMillis() > deadline) fail("timed out waiting for $what")
            Thread.sleep(10)
        }
    }

    private val icon = Art("image/png", byteArrayOf(1, 2, 3))
    private val cover = Art("image/jpeg", byteArrayOf(4, 5, 6))

    private fun activity(id: String, title: String, iconKey: String? = null, artKey: String? = null) = json(
        buildString {
            append("""{"id":"$id","kind":"generic","app":{"id":"a","name":"A"},"title":"$title"""")
            if (iconKey != null) append(""","iconKey":"$iconKey"""")
            if (artKey != null) append(""","artKey":"$artKey"""")
            append(""","actions":[{"id":"notif.0","label":"Pause"}],"updatedMs":1}""")
        },
    )

    private fun pair(session: ResponderSession, c: Client) {
        assertEquals(json("""{"t":"ready","paired":false,"now":0}""").keys, c.recvType("ready").keys)
        c.send("""{"t":"ready","paired":false,"now":1}""")
        c.send("""{"t":"pair_request"}""")
        waitFor("pairing state") { session.state == ResponderSession.State.PAIRING }
        assertEquals(c.ch.sas, session.sas)
        c.send("""{"t":"pair_decision","accepted":true}""")
        waitFor("remote decision") { session.remoteDecision == true }
        assertEquals(ResponderSession.State.PAIRING, session.state)
        session.decidePairing(true)
        assertEquals(json("""{"t":"pair_decision","accepted":true}"""), c.recvType("pair_decision"))
    }

    @Test
    fun pairingThenSnapshotArtOrderAndStreaming() {
        hub.upsert(activity("x", "First", iconKey = icon.key), listOf(icon))
        val laptop = KeyPair.generate()
        val (session, c) = connect(laptop)
        val ready = c.recvType("ready")
        assertEquals(false, ready.bool("paired"))
        assertTrue(ready.long("now")!! > 0)
        c.send("""{"t":"ready","paired":false,"now":1}""")
        // Non-pairing messages before pairing are refused.
        c.send("""{"t":"action","req":1,"activity":"x","action":"notif.0"}""")
        assertEquals("not_paired", c.recvType("error").str("code"))
        c.send("""{"t":"unpair"}""")
        assertEquals("not_paired", c.recvType("error").str("code"))
        assertTrue(host.actions.isEmpty())

        c.send("""{"t":"pair_request"}""")
        waitFor("pairing") { session.state == ResponderSession.State.PAIRING }
        assertEquals(c.ch.sas, session.sas)
        session.decidePairing(true)
        assertEquals(json("""{"t":"pair_decision","accepted":true}"""), c.recvType("pair_decision"))
        assertTrue("not pinned before both decisions", host.pins.isEmpty())
        c.send("""{"t":"pair_decision","accepted":true}""")

        val art = c.recv()
        assertEquals("art", art.type)
        assertEquals(icon.key, art.str("key"))
        assertEquals("image/png", art.str("mime"))
        assertEquals(b64e(byteArrayOf(1, 2, 3)), art.str("data"))
        val snapshot = c.recvType("snapshot")
        assertEquals(listOf("x"), (snapshot["activities"] as JsonArray).map { (it as JsonObject).str("id") })
        assertEquals(laptop.public.toList(), host.pins.getValue(deviceId(laptop.public)).static.toList())
        assertEquals(ResponderSession.State.PAIRED, session.state)

        // Same icon again: no second art message.
        hub.upsert(activity("y", "Second", iconKey = icon.key))
        val up = c.recv()
        assertEquals("upsert", up.type)
        assertEquals("y", (up["activity"] as JsonObject).str("id"))
        // New art key: art precedes the upsert.
        hub.upsert(activity("y", "Second", iconKey = icon.key, artKey = cover.key), listOf(cover))
        assertEquals(cover.key, c.recv().also { assertEquals("art", it.type) }.str("key"))
        assertEquals(cover.key, (c.recvType("upsert")["activity"] as JsonObject).str("artKey"))
        hub.remove("x")
        assertEquals(json("""{"t":"remove","id":"x"}"""), c.recvType("remove"))
    }

    @Test
    fun pairedPeerReconnectsWithoutPairing() {
        val laptop = KeyPair.generate()
        host.pins[deviceId(laptop.public)] = Peer(deviceId(laptop.public), "Laptop", "linux", laptop.public)
        hub.upsert(activity("x", "First"))
        val (session, c) = connect(laptop)
        assertEquals(true, c.recvType("ready").bool("paired"))
        c.send("""{"t":"ready","paired":true,"now":1}""")
        val snap = c.recvType("snapshot")
        assertEquals(1, (snap["activities"] as JsonArray).size)
        assertEquals(ResponderSession.State.PAIRED, session.state)
    }

    @Test
    fun peerClaimingPairedWithoutPinIsNotPaired() {
        val (session, c) = connect()
        assertEquals(false, c.recvType("ready").bool("paired"))
        c.send("""{"t":"ready","paired":true,"now":1}""")
        c.send("""{"t":"action","req":1,"activity":"x","action":"notif.0"}""")
        assertEquals("not_paired", c.recvType("error").str("code"))
        assertEquals(ResponderSession.State.UNPAIRED, session.state)
    }

    @Test
    fun actionsProduceResults() {
        hub.upsert(
            json(
                """{"id":"media:p","kind":"media","app":{"id":"p","name":"P"},"title":"T",
                "media":{"state":"paused","positionMs":5,"seekable":true},"actions":[{"id":"media.play","label":"Play"}]}""",
            ),
        )
        val (session, c) = connect()
        pair(session, c)
        c.recvType("snapshot")
        c.send("""{"t":"action","req":7,"activity":"media:p","action":"media.play"}""")
        assertEquals(json("""{"t":"action_result","req":7,"ok":true}"""), c.recvType("action_result"))
        c.send("""{"t":"action","req":8,"activity":"media:p","action":"media.seek","value":1234}""")
        assertEquals(json("""{"t":"action_result","req":8,"ok":true}"""), c.recvType("action_result"))
        assertEquals(Triple("media:p", "media.seek", JsonPrimitive(1234) as JsonElement?), host.actions[1])
        c.send("""{"t":"action","req":9,"activity":"media:p","action":"media.next"}""")
        assertEquals(json("""{"t":"action_result","req":9,"ok":false,"error":"unknown_action"}"""), c.recvType("action_result"))
        c.send("""{"t":"action","req":10,"activity":"nope","action":"media.play"}""")
        assertEquals("unknown_activity", c.recvType("action_result").str("error"))
        host.actionOutcome = false to "boom"
        c.send("""{"t":"action","req":11,"activity":"media:p","action":"media.play"}""")
        assertEquals(json("""{"t":"action_result","req":11,"ok":false,"error":"boom"}"""), c.recvType("action_result"))
        assertEquals(3, host.actions.size)
    }

    @Test
    fun localRejectSendsDecisionAndCloses() {
        val (session, c) = connect()
        c.recvType("ready")
        c.send("""{"t":"pair_request"}""")
        waitFor("pairing") { session.state == ResponderSession.State.PAIRING }
        session.decidePairing(false)
        assertEquals(json("""{"t":"pair_decision","accepted":false}"""), c.recvType("pair_decision"))
        assertEquals("closed", runCatching { c.recv() }.exceptionOrNull().let { (it as? ProtocolError)?.code ?: "closed" })
        waitFor("closed") { session.state == ResponderSession.State.CLOSED }
        assertTrue(host.pins.isEmpty())
    }

    @Test
    fun remoteRejectCloses() {
        val (session, c) = connect()
        c.recvType("ready")
        c.send("""{"t":"pair_request"}""")
        c.send("""{"t":"pair_decision","accepted":false}""")
        waitFor("closed") { session.state == ResponderSession.State.CLOSED }
        assertEquals("pairing rejected by peer", session.closeReason)
        assertTrue(host.pins.isEmpty())
    }

    @Test
    fun unpairRemovesPinAndCloses() {
        val (session, c) = connect()
        pair(session, c)
        c.recvType("snapshot")
        assertEquals(1, host.pins.size)
        c.send("""{"t":"unpair"}""")
        waitFor("closed") { session.state == ResponderSession.State.CLOSED }
        assertTrue(host.pins.isEmpty())
    }

    @Test
    fun localForgetSendsUnpair() {
        val (session, c) = connect()
        pair(session, c)
        c.recvType("snapshot")
        session.unpairAndClose()
        assertEquals(json("""{"t":"unpair"}"""), c.recvType("unpair"))
        waitFor("closed") { session.state == ResponderSession.State.CLOSED }
    }

    @Test
    fun pingIsAnsweredEvenUnpairedAndUnknownTypesIgnored() {
        val (_, c) = connect()
        c.recvType("ready")
        c.send("""{"t":"ping","req":3}""")
        assertEquals(json("""{"t":"pong","req":3}"""), c.recv())
        c.send("""{"t":"ready","paired":false,"now":1}""")
        c.send("""{"t":"pair_request"}""")
        c.send("""{"t":"ping","req":4}""")
        assertEquals(json("""{"t":"pong","req":4}"""), c.recv())
    }

    @Test
    fun keepaliveAndIdleTimeout() {
        val (session, c) = connect(timeouts = SessionTimeouts(pingAfterMs = 200, idleMs = 1200, tickMs = 50))
        c.recvType("ready")
        val ping = c.recv()
        assertEquals("ping", ping.type)
        assertTrue(ping.long("req")!! >= 1)
        waitFor("idle close") { session.state == ResponderSession.State.CLOSED }
        assertEquals("idle timeout", session.closeReason)
    }

    @Test
    fun pairingTimesOut() {
        val (session, c) = connect(timeouts = SessionTimeouts(pairingMs = 300, tickMs = 50))
        c.recvType("ready")
        c.send("""{"t":"pair_request"}""")
        waitFor("closed") { session.state == ResponderSession.State.CLOSED }
        assertEquals("pairing timeout", session.closeReason)
    }

    @Test
    fun tamperedRecordClosesSession() {
        val (session, c) = connect()
        c.recvType("ready")
        // Raw frame that is not a valid record under k_i2r.
        c.ch.frames.writeFrame(ByteArray(40) { 7 })
        waitFor("closed") { session.state == ResponderSession.State.CLOSED }
        assertEquals("decrypt_failed", session.closeReason)
    }

    @Test
    fun multipleSessionsReceiveUpdates() {
        val (s1, c1) = connect()
        pair(s1, c1)
        c1.recvType("snapshot")
        val (s2, c2) = connect()
        pair(s2, c2)
        c2.recvType("snapshot")
        assertEquals(2, host.pins.size)
        hub.upsert(activity("z", "Both"))
        assertEquals("z", (c1.recvType("upsert")["activity"] as JsonObject).str("id"))
        assertEquals("z", (c2.recvType("upsert")["activity"] as JsonObject).str("id"))
        assertFalse(s1.id == s2.id)
    }

    @Test
    fun byeClosesSession() {
        val (session, c) = connect()
        c.recvType("ready")
        c.send("""{"t":"bye","reason":"test"}""")
        waitFor("closed") { session.state == ResponderSession.State.CLOSED }
        assertNull(host.pins.values.firstOrNull())
    }
}
