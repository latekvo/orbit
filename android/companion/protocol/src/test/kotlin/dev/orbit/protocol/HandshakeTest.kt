package dev.orbit.protocol

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonPrimitive
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Test
import java.net.ServerSocket
import java.net.Socket
import java.util.concurrent.Callable
import java.util.concurrent.Executors
import java.util.concurrent.Future
import java.util.concurrent.TimeUnit

class HandshakeTest {
    private val pool = Executors.newCachedThreadPool()

    /** Runs [server] on an accepted loopback socket; returns its future and a connected client stream. */
    private fun <T> loopback(server: (FrameStream) -> T): Pair<Future<T>, FrameStream> {
        val ss = ServerSocket(0)
        val f = pool.submit(Callable { ss.accept().use { server(FrameStream(SocketByteStream(it))) }.also { ss.close() } })
        val sock = Socket("127.0.0.1", ss.localPort).apply { soTimeout = 5000 }
        return f to FrameStream(SocketByteStream(sock))
    }

    private fun failureCode(f: Future<*>): String =
        (runCatching { f.get(5, TimeUnit.SECONDS) }.exceptionOrNull()?.cause as ProtocolError).code

    @Test
    fun loopbackHandshakeAgreesOnKeysAndSas() {
        val phone = KeyPair.generate()
        val laptop = KeyPair.generate()
        val (f, client) = loopback { fs ->
            val ch = Handshake.respond(fs, phone, "Phone", "android")
            ch.send(json("""{"t":"ready","paired":false,"now":1}"""))
            ch to ch.recv()
        }
        val ch = Handshake.initiate(client, laptop, "Laptop", "macos")
        assertEquals(json("""{"t":"ready","paired":false,"now":1}"""), ch.recv())
        ch.send(json("""{"t":"pair_request"}"""))
        val (rch, msg) = f.get(5, TimeUnit.SECONDS)
        assertEquals("pair_request", msg.type)
        assertEquals(ch.sas, rch.sas)
        assertEquals(6, ch.sas.length)
        assertEquals(deviceId(laptop.public), rch.peer.id)
        assertArrayEquals(phone.public, ch.peer.static)
        assertEquals("android", ch.peer.platform)
    }

    @Test
    fun commitMismatchIsRejected() {
        val (f, client) = loopback { Handshake.respond(it, KeyPair.generate(), "P", "android") }
        val honest = Handshake.helloBody("initiator", KeyPair.generate(), KeyPair.generate(), ByteArray(16), "L", "linux")
        client.writeFrame("""{"t":"commit","v":1,"c":"${b64e(sha256(honest))}"}""".toByteArray())
        client.readFrame()
        // Same hello re-serialized with a space: the hash covers exact bytes, so it must fail.
        client.writeFrame(String(honest).replace(",\"v\"", ", \"v\"").toByteArray())
        assertEquals("bad_commit", decode(client.readFrame()).str("code"))
        assertEquals("bad_commit", failureCode(f))
    }

    @Test
    fun unsupportedVersionGetsError() {
        val (f, client) = loopback { Handshake.respond(it, KeyPair.generate(), "P", "android") }
        client.writeFrame("""{"t":"commit","v":99,"c":""}""".toByteArray())
        val reply = decode(client.readFrame())
        assertEquals("unsupported_version", reply.str("code"))
        assertEquals(JsonArray(listOf(JsonPrimitive(1))), reply["supported"])
        assertEquals("unsupported_version", failureCode(f))
    }

    @Test
    fun idNotMatchingStaticIsRejected() {
        val (f, client) = loopback { Handshake.respond(it, KeyPair.generate(), "P", "android") }
        val hello = String(Handshake.helloBody("initiator", KeyPair.generate(), KeyPair.generate(), ByteArray(16), "L", "linux"))
            .replace(Regex("\"id\":\"[0-9a-f]{32}\""), "\"id\":\"${"0".repeat(32)}\"").toByteArray()
        client.writeFrame("""{"t":"commit","v":1,"c":"${b64e(sha256(hello))}"}""".toByteArray())
        client.readFrame()
        client.writeFrame(hello)
        assertEquals("bad_hello", failureCode(f))
    }

    @Test
    fun invalidPointIsRejected() {
        val bad = ByteArray(65).also { it[0] = 4; it[64] = 1 }
        assertThrows(IllegalArgumentException::class.java) { validatePublicKey(bad) }
        assertThrows(IllegalArgumentException::class.java) { validatePublicKey(KeyPair.generate().public.copyOf(33)) }
    }

    @Test
    fun responderValidatesInitiatorRole() {
        val (f, client) = loopback { Handshake.respond(it, KeyPair.generate(), "P", "android") }
        val hello = Handshake.helloBody("responder", KeyPair.generate(), KeyPair.generate(), ByteArray(16), "L", "linux")
        client.writeFrame("""{"t":"commit","v":1,"c":"${b64e(sha256(hello))}"}""".toByteArray())
        client.readFrame()
        client.writeFrame(hello)
        assertEquals("bad_hello", failureCode(f))
    }

    @Test
    fun tamperedRecordFailsAuthentication() {
        val key = ByteArray(32) { it.toByte() }
        val rec = CipherState(key).encrypt("""{"t":"ping","req":1}""".toByteArray())
        rec[0] = (rec[0].toInt() xor 1).toByte()
        assertEquals("decrypt_failed", assertThrows(ProtocolError::class.java) { CipherState(key).decrypt(rec) }.code)
        // Replayed/reordered records fail too: the counter is implicit.
        val tx = CipherState(key)
        tx.encrypt(byteArrayOf(1))
        val second = tx.encrypt(byteArrayOf(2))
        assertEquals("decrypt_failed", assertThrows(ProtocolError::class.java) { CipherState(key).decrypt(second) }.code)
    }

    @Test
    fun frameLengthLimitsAreEnforced() {
        val (f, client) = loopback { it.readFrame() }
        client.stream.output.write(byteArrayOf(0, 0x10, 0, 1)) // 1 MiB + 1
        client.stream.output.flush()
        assertEquals("bad_frame", failureCode(f))
        assertThrows(ProtocolError::class.java) { client.writeFrame(ByteArray(0)) }
    }
}
