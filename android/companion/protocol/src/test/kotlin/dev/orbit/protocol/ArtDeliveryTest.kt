package dev.orbit.protocol

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import org.junit.After
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Assert.fail
import org.junit.Test
import java.io.OutputStream
import java.net.ServerSocket
import java.net.Socket
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledThreadPoolExecutor
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

/** Art delivery: behind control traffic, superseded art dropped, per-transport variants. */
class ArtDeliveryTest {
    private val scheduler = ScheduledThreadPoolExecutor(2).apply { removeOnCancelPolicy = true }
    private val threads = Executors.newCachedThreadPool()
    private val server = ServerSocket(0)
    private val phone = KeyPair.generate()
    private val laptop = KeyPair.generate()
    private val hub = ActivityHub()
    private val opened = mutableListOf<AutoCloseable>()

    @After
    fun tearDown() {
        opened.forEach { runCatching { it.close() } }
        server.close()
        threads.shutdownNow()
        scheduler.shutdownNow()
    }

    /** Output that blocks writers while closed, standing in for a link that is busy draining. */
    private class GatedStream(private val inner: ByteStream) : ByteStream {
        private val lock = Object()
        private var open = true
        @Volatile var blockedWriters = 0
        override val input = inner.input
        override val output = object : OutputStream() {
            override fun write(b: Int) = write(byteArrayOf(b.toByte()), 0, 1)
            override fun write(b: ByteArray, off: Int, len: Int) {
                synchronized(lock) {
                    while (!open) {
                        blockedWriters++
                        try { lock.wait() } finally { blockedWriters-- }
                    }
                }
                inner.output.write(b, off, len)
            }
            override fun flush() = inner.output.flush()
        }
        fun close(gate: Boolean) = synchronized(lock) { open = !gate; lock.notifyAll() }
        override fun close() = inner.close()
    }

    private class Link(val session: ResponderSession, val gate: GatedStream, val ch: SecureChannel) {
        /** Pings are answered (art waits for their pongs) unless this is off; the last one is kept. */
        var pongs = true
        var unanswered: Long? = null

        /** The next message that is not a ping. */
        fun recv(): JsonObject {
            while (true) {
                val m = ch.recv()
                if (m.type != "ping") return m
                if (pongs) pong(m.long("req")!!) else unanswered = m.long("req")
            }
        }

        fun pong(req: Long) = ch.send(json("""{"t":"pong","req":$req}"""))
    }

    private fun connect(
        transport: String = "lan",
        host: SessionHost = FakeHost(),
        artSizer: ArtSizer? = null,
        timeouts: SessionTimeouts = SessionTimeouts(),
        ready: String = """{"t":"ready","paired":true,"now":1}""",
    ): Link {
        val created = ArrayBlockingQueue<Pair<ResponderSession, GatedStream>>(1)
        threads.execute {
            val gate = GatedStream(SocketByteStream(server.accept()))
            val s = ResponderSession(gate, transport, phone, "Phone", "android", hub, host, scheduler, timeouts = timeouts, artSizer = artSizer)
            created.put(s to gate)
            s.run()
        }
        val sock = Socket("127.0.0.1", server.localPort).apply { soTimeout = 5000 }
        opened += sock
        val ch = Handshake.initiate(FrameStream(SocketByteStream(sock)), laptop, "Laptop", "linux")
        val (session, gate) = created.take()
        return Link(session, gate, ch).also {
            assertEquals("ready", it.recv().type)
            ch.send(json(ready))
        }
    }

    private fun pinnedHost(): FakeHost = FakeHost().apply { pin(Peer(deviceId(laptop.public), "Laptop", "linux", laptop.public)) }

    private fun waitFor(what: String, cond: () -> Boolean) {
        val deadline = System.currentTimeMillis() + 5000
        while (!cond()) {
            if (System.currentTimeMillis() > deadline) fail("timed out waiting for $what")
            Thread.sleep(5)
        }
    }

    private fun image(mime: String, seed: Int, size: Int = 100) = Art(mime, ByteArray(size) { (seed + it).toByte() })

    private fun media(id: String = "m", iconKey: String? = null, artKey: String? = null) = json(
        buildString {
            append("""{"id":"$id","kind":"media","app":{"id":"p","name":"P"},"title":"T"""")
            if (iconKey != null) append(""","iconKey":"$iconKey"""")
            if (artKey != null) append(""","artKey":"$artKey"""")
            append(""","media":{"state":"playing"},"actions":[{"id":"media.next","label":"Next"}],"updatedMs":1}""")
        },
    )

    private val JsonObject.activity get() = this["activity"] as JsonObject

    @Test
    fun actionResultOvertakesArtQueuedBeforeIt() {
        val resultQueued = CountDownLatch(1)
        val host = object : SessionHost by pinnedHost() {
            override fun performAction(activity: JsonObject, actionId: String, value: JsonElement?, done: (Boolean, String?) -> Unit) {
                done(true, null)
                resultQueued.countDown()
            }
        }
        hub.upsert(media())
        val link = connect(host = host)
        assertEquals("snapshot", link.recv().type)

        val cover = image("image/jpeg", 1)
        link.gate.close(gate = true)
        hub.upsert(media(artKey = cover.key), listOf(cover))
        waitFor("sender blocked on the link") { link.gate.blockedWriters > 0 }
        link.ch.send(json("""{"t":"action","req":1,"activity":"m","action":"media.next"}"""))
        assertEquals(true, resultQueued.await(5, TimeUnit.SECONDS))
        link.gate.close(gate = false)

        val got = List(3) { link.recv() }
        assertEquals(listOf("upsert", "action_result", "art"), got.map { it.type })
        assertEquals(cover.key, got[0].activity.str("artKey"))
        assertEquals(cover.key, got[2].str("key"))
    }

    @Test
    fun artSupersededBeforeItWasSentIsDropped() {
        hub.upsert(media())
        val link = connect(host = pinnedHost())
        assertEquals("snapshot", link.recv().type)

        val first = image("image/jpeg", 1)
        val second = image("image/jpeg", 2)
        link.gate.close(gate = true)
        hub.upsert(media(artKey = first.key), listOf(first))
        waitFor("sender blocked on the link") { link.gate.blockedWriters > 0 }
        hub.upsert(media(artKey = second.key), listOf(second))
        link.gate.close(gate = false)

        val got = mutableListOf<JsonObject>()
        while (got.none { it.type == "art" }) got += link.recv()
        assertEquals(listOf(first.key, second.key), got.filter { it.type == "upsert" }.map { it.activity.str("artKey") })
        assertEquals(listOf(second.key), got.filter { it.type == "art" }.map { it.str("key") })

        // Removed before its art went out.
        val removed = image("image/jpeg", 3)
        link.gate.close(gate = true)
        hub.upsert(media("other", artKey = removed.key), listOf(removed))
        waitFor("sender blocked on the link") { link.gate.blockedWriters > 0 }
        hub.remove("other")
        link.gate.close(gate = false)
        assertEquals("upsert", link.recv().type)
        assertEquals("remove", link.recv().type)
        // Queued art goes out back to back, so art that should have been dropped precedes the pong.
        link.ch.send(json("""{"t":"ping","req":99}"""))
        assertEquals("pong", link.recv().type)
    }

    @Test
    fun artOverflowDropsTheOldestArtAndKeepsTheSession() {
        val link = connect(host = pinnedHost(), timeouts = SessionTimeouts(maxQueuedArt = 2))
        assertEquals("snapshot", link.recv().type)

        val covers = List(4) { image("image/jpeg", it) }
        link.gate.close(gate = true)
        hub.upsert(media("a0", artKey = covers[0].key), listOf(covers[0]))
        waitFor("sender blocked on the link") { link.gate.blockedWriters > 0 }
        covers.drop(1).forEachIndexed { i, c -> hub.upsert(media("a${i + 1}", artKey = c.key), listOf(c)) }
        link.gate.close(gate = false)

        val got = mutableListOf<JsonObject>()
        while (got.count { it.type == "art" } < 2) got += link.recv()
        assertEquals(4, got.count { it.type == "upsert" })
        assertEquals(listOf(covers[2].key, covers[3].key), got.filter { it.type == "art" }.map { it.str("key") })
        assertEquals(ResponderSession.State.PAIRED, link.session.state)
    }

    @Test
    fun artGoesOutInPartsOneFramePerRoundTrip() {
        val partsReady = """{"t":"ready","paired":true,"artParts":true,"now":1}"""
        val cover = Art("image/jpeg", ByteArray(3 * ResponderSession.ART_PART_BYTES + 100) { it.toByte() })
        hub.upsert(media(artKey = cover.key), listOf(cover))
        val link = connect(host = pinnedHost(), ready = partsReady)
        link.pongs = false
        assertEquals("snapshot", link.recv().type)
        val first = link.recv()
        assertEquals(listOf("art", 0L, 4L), listOf(first.type, first.long("part"), first.long("parts")))
        // Its ping is not answered: no further art, while other messages still flow.
        link.ch.send(json("""{"t":"ping","req":7}"""))
        assertEquals("pong", link.recv().type)
        link.pongs = true
        link.pong(link.unanswered!!)
        val rest = List(3) { link.recv() }
        assertEquals(listOf(1L, 2L, 3L), rest.map { it.long("part") })
        val data = (listOf(first) + rest).map { b64d(it.str("data")!!) }
        assertEquals(listOf(4096, 4096, 4096, 100), data.map { it.size })
        assertArrayEquals(cover.data, data.reduce { a, b -> a + b })
        assertEquals(setOf(cover.key), (listOf(first) + rest).map { it.str("key") }.toSet())

        // A peer that does not reassemble parts gets the whole image in one frame.
        val whole = connect(host = pinnedHost())
        assertEquals("snapshot", whole.recv().type)
        val art = whole.recv()
        assertEquals(listOf(null, null), listOf(art["part"], art["parts"]))
        assertArrayEquals(cover.data, b64d(art.str("data")!!))
    }

    @Test
    fun artTheLaptopHoldsIsNotResent() {
        val held = image("image/png", 1)
        val fresh = image("image/jpeg", 2)
        hub.upsert(media(iconKey = held.key, artKey = fresh.key), listOf(held, fresh)) // the icon would go first
        val link = connect(host = pinnedHost(), ready = """{"t":"ready","paired":true,"art":["${held.key}"],"now":1}""")
        assertEquals("snapshot", link.recv().type)
        assertEquals(fresh.key, link.recv().also { assertEquals("art", it.type) }.str("key"))
        link.ch.send(json("""{"t":"ping","req":7}"""))
        assertEquals("pong", link.recv().type)
    }

    @Test
    fun imageSupersededWhileGoingOutInPartsIsAbandoned() {
        val partsReady = """{"t":"ready","paired":true,"artParts":true,"now":1}"""
        val first = Art("image/jpeg", ByteArray(3 * ResponderSession.ART_PART_BYTES) { 1 })
        val second = image("image/jpeg", 2)
        hub.upsert(media(artKey = first.key), listOf(first))
        val link = connect(host = pinnedHost(), ready = partsReady)
        link.pongs = false
        assertEquals("snapshot", link.recv().type)
        assertEquals(0L, link.recv().long("part"))
        hub.upsert(media(artKey = second.key), listOf(second))
        assertEquals("upsert", link.recv().type)
        link.pongs = true
        link.pong(link.unanswered!!)
        val next = link.recv()
        assertEquals(listOf("art", second.key), listOf(next.type, next.str("key")))
    }

    /** Variants whose bytes name the size they were made for. */
    private class FakeTranscoder : ArtTranscoder {
        val calls = AtomicInteger()
        override fun scale(art: Art, maxPx: Int, maxBytes: Int): Art {
            calls.incrementAndGet()
            return Art(art.mime, "${art.key}@$maxPx".toByteArray())
        }
    }

    private fun snapshotAndArt(transport: String, sizer: ArtSizer): Pair<JsonObject, Map<String, JsonObject>> {
        val link = connect(transport, pinnedHost(), sizer)
        val snapshot = link.recv()
        assertEquals("snapshot", snapshot.type)
        val arts = List(2) { link.recv() }.onEach { assertEquals("art", it.type) }.associateBy { it.str("key")!! }
        link.session.close("done")
        return (snapshot["activities"] as JsonArray).single() as JsonObject to arts
    }

    @Test
    fun bleSessionsGetSmallVariantsAndLanSessionsTheStoredArt() {
        val icon = image("image/png", 1, size = 5000)
        val cover = image("image/jpeg", 2, size = 20_000)
        hub.upsert(media(iconKey = icon.key, artKey = cover.key), listOf(icon, cover))
        val sizer = ArtSizer(FakeTranscoder())
        val smallIcon = Art("image/png", "${icon.key}@64".toByteArray())
        val smallCover = Art("image/jpeg", "${cover.key}@128".toByteArray())

        val (bleActivity, bleArt) = snapshotAndArt("ble", sizer)
        assertEquals(smallIcon.key, bleActivity.str("iconKey"))
        assertEquals(smallCover.key, bleActivity.str("artKey"))
        assertEquals(setOf(smallIcon.key, smallCover.key), bleArt.keys)
        assertArrayEquals(smallCover.data, b64d(bleArt.getValue(smallCover.key).str("data")!!))
        assertEquals("image/png", bleArt.getValue(smallIcon.key).str("mime"))

        val (lanActivity, lanArt) = snapshotAndArt("lan", sizer)
        assertEquals(icon.key, lanActivity.str("iconKey"))
        assertEquals(cover.key, lanActivity.str("artKey"))
        assertArrayEquals(cover.data, b64d(lanArt.getValue(cover.key).str("data")!!))
    }

    @Test
    fun sizerPicksTheSmallestImageWithinBudgetAndCachesIt() {
        val (artPx, artBytes) = ArtSizer.BLE_ART
        assertEquals(128 to 6 * 1024, ArtSizer.BLE_ART)
        assertEquals(64 to 4 * 1024, ArtSizer.BLE_ICON)
        val fake = FakeTranscoder()
        val sizer = ArtSizer(fake)
        val cover = image("image/jpeg", 1, size = artBytes + 1)
        val small = sizer.forTransport(cover, "artKey", "ble")!!
        assertEquals("${cover.key}@$artPx", String(small.data))
        assertSame(small, sizer.forTransport(cover, "artKey", "ble"))
        assertEquals(1, fake.calls.get())
        assertSame(cover, sizer.forTransport(cover, "artKey", "lan"))

        // Nothing within budget: omitted. A failing transcoder falls back to art that fits.
        val tooBig = ArtSizer(ArtTranscoder { a, _, _ -> Art(a.mime, ByteArray(artBytes + 1)) })
        assertNull(tooBig.forTransport(cover, "artKey", "ble"))
        val broken = ArtSizer(ArtTranscoder { _, _, _ -> throw IllegalStateException("codec") })
        val fits = image("image/jpeg", 2, size = 10)
        assertSame(fits, broken.forTransport(fits, "artKey", "ble"))
        assertNull(broken.forTransport(cover, "artKey", "ble"))
    }
}
