package dev.orbit.protocol

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Test
import java.io.File
import java.net.ServerSocket
import java.net.Socket
import java.util.concurrent.Callable
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

class VectorsTest {
    private val v: JsonObject = Json.parseToJsonElement(File(System.getProperty("orbit.vectors")).readText()).jsonObject
    private fun s(key: String) = v[key]!!.jsonPrimitive.content
    private fun obj(key: String) = v[key]!!.jsonObject
    private val keys = obj("private_keys_hex").mapValues { KeyPair.fromScalar(hexToBytes(it.value.jsonPrimitive.content)) }

    @Test
    fun publicKeysAndDeviceIds() {
        for ((name, pub) in obj("public_keys_b64")) {
            assertEquals(name, pub.jsonPrimitive.content, b64e(keys.getValue(name).public))
        }
        assertEquals(s("device_id_initiator"), deviceId(keys.getValue("static_i").public))
        assertEquals(s("device_id_responder"), deviceId(keys.getValue("static_r").public))
    }

    @Test
    fun helloAndCommitBodiesAreByteExact() {
        val helloR = Handshake.helloBody(
            "responder", keys.getValue("static_r"), keys.getValue("eph_r"),
            hexToBytes(s("nonce_responder_hex")), "Test Phone", "android",
        )
        val helloI = Handshake.helloBody(
            "initiator", keys.getValue("static_i"), keys.getValue("eph_i"),
            hexToBytes(s("nonce_initiator_hex")), "Test Laptop", "macos",
        )
        assertEquals(s("hello_responder_body"), String(helloR, Charsets.UTF_8))
        assertEquals(s("hello_initiator_body"), String(helloI, Charsets.UTF_8))
        val commit = Json.parseToJsonElement(s("commit_body")).jsonObject
        assertEquals(commit.str("c"), b64e(sha256(helloI)))
    }

    @Test
    fun keyScheduleAndSas() {
        val th = transcriptHash(
            s("commit_body").toByteArray(), s("hello_responder_body").toByteArray(), s("hello_initiator_body").toByteArray(),
        )
        assertEquals(s("transcript_hash_hex"), th.toHex())
        val dhEe = keys.getValue("eph_i").dh(keys.getValue("eph_r").public)
        val dhSs = keys.getValue("static_r").dh(keys.getValue("static_i").public)
        assertEquals(s("dh_ee_hex"), dhEe.toHex())
        assertEquals(s("dh_ss_hex"), dhSs.toHex())
        // ECDH is symmetric.
        assertArrayEquals(dhEe, keys.getValue("eph_r").dh(keys.getValue("eph_i").public))
        val (kI2r, kR2i) = sessionKeys(th, dhEe, dhSs)
        assertEquals(s("k_i2r_hex"), kI2r.toHex())
        assertEquals(s("k_r2i_hex"), kR2i.toHex())
        assertEquals(s("sas"), sasCode(th))
    }

    @Test
    fun recordsDecryptAndReEncrypt() {
        for ((keyName, recName) in listOf("k_i2r_hex" to "records_i2r", "k_r2i_hex" to "records_r2i")) {
            val key = hexToBytes(s(keyName))
            val dec = CipherState(key)
            val enc = CipherState(key)
            for (rec in v[recName] as JsonArray) {
                val r = rec.jsonObject
                assertEquals(r.long("counter"), dec.counter)
                val record = hexToBytes(r.str("record_hex")!!)
                assertEquals(r.str("plaintext"), String(dec.decrypt(record), Charsets.UTF_8))
                assertEquals(r.str("record_hex"), enc.encrypt(r.str("plaintext")!!.toByteArray()).toHex())
            }
        }
    }

    /** Both handshake roles over a real socket with the vector keys reproduce every wire byte. */
    @Test
    fun deterministicHandshakeOverTcp() {
        val server = ServerSocket(0)
        val pool = Executors.newFixedThreadPool(2)
        val recorded = mutableListOf<ByteArray>()
        val written = mutableListOf<ByteArray>()
        val responder = pool.submit(Callable {
            server.accept().use { sock ->
                val frames = RecordingFrames(SocketByteStream(sock), recorded, written)
                val ch = Handshake.respond(
                    frames, keys.getValue("static_r"), "Test Phone", "android",
                    eph = keys.getValue("eph_r"), nonce = hexToBytes(s("nonce_responder_hex")),
                )
                ch.send(Json.parseToJsonElement("""{"t":"ready","paired":false,"now":1760000000001}""").jsonObject)
                val first = ch.recv()
                Triple(ch, first, ch.recv())
            }
        })
        Socket("127.0.0.1", server.localPort).use { sock ->
            val frames = FrameStream(SocketByteStream(sock))
            val ch = Handshake.initiate(
                frames, keys.getValue("static_i"), "Test Laptop", "macos",
                eph = keys.getValue("eph_i"), nonce = hexToBytes(s("nonce_initiator_hex")),
            )
            assertEquals(s("sas"), ch.sas)
            assertEquals(s("device_id_responder"), ch.peer.id)
            assertEquals("""{"t":"ready","paired":false,"now":1760000000001}""", ch.recv().toString())
            for (rec in v["records_i2r"] as JsonArray) {
                ch.send(Json.parseToJsonElement(rec.jsonObject.str("plaintext")!!).jsonObject)
            }
            val (rch, first, second) = responder.get(10, TimeUnit.SECONDS)
            assertEquals(s("sas"), rch.sas)
            assertEquals("Test Laptop", rch.peer.name)
            assertEquals("macos", rch.peer.platform)
            assertEquals("ready", first.type)
            assertEquals("""{"t":"pair_request"}""", second.toString())
        }
        // Responder received exactly: commit, hello_I, then the two vector records.
        assertEquals(s("commit_body"), String(recorded[0]))
        assertEquals(s("hello_initiator_body"), String(recorded[1]))
        val i2r = (v["records_i2r"] as JsonArray).map { it.jsonObject.str("record_hex") }
        assertEquals(i2r, recorded.drop(2).map { it.toHex() })
        // Responder sent exactly: hello_R, then the vector's first R->I record.
        assertEquals(s("hello_responder_body"), String(written[0]))
        assertEquals((v["records_r2i"] as JsonArray)[0].jsonObject.str("record_hex"), written[1].toHex())
        pool.shutdownNow()
        server.close()
    }

    /** Frames wrapper that records every frame body read and written. */
    private class RecordingFrames(stream: ByteStream, val read: MutableList<ByteArray>, val written: MutableList<ByteArray>) :
        FrameStream(stream) {
        override fun readFrame(): ByteArray = super.readFrame().also { read += it }
        override fun writeFrame(body: ByteArray) {
            written += body
            super.writeFrame(body)
        }
    }
}
