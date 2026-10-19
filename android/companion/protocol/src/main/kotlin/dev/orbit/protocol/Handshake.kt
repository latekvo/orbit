package dev.orbit.protocol

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import java.io.Closeable
import java.io.DataInputStream
import java.io.EOFException
import java.io.InputStream
import java.io.OutputStream
import java.nio.ByteBuffer
import java.security.SecureRandom

const val VERSION = 1
const val MAX_FRAME = 1 shl 20

/** Largest plaintext handshake frame (spec 2); hello bodies are a few hundred bytes. */
const val MAX_HANDSHAKE_FRAME = 4096

class ProtocolError(val code: String, override val message: String = "") :
    Exception(if (message.isEmpty()) code else "$code: $message")

/** Compact JSON in insertion order (the exact form the reference implementation emits). */
fun encode(msg: JsonObject): ByteArray = msg.toString().toByteArray(Charsets.UTF_8)

fun decode(body: ByteArray): JsonObject {
    val el = try {
        Json.parseToJsonElement(Charsets.UTF_8.newDecoder().decode(ByteBuffer.wrap(body)).toString())
    } catch (e: Exception) {
        throw ProtocolError("bad_message", e.message ?: "invalid JSON")
    }
    if (el !is JsonObject || el.str("t") == null) {
        throw ProtocolError("bad_message", "message must be an object with a string 't'")
    }
    return el
}

fun JsonObject.str(key: String): String? = (this[key] as? JsonPrimitive)?.takeIf { it.isString }?.content

fun JsonObject.long(key: String): Long? = (this[key] as? JsonPrimitive)?.takeIf { !it.isString }?.content?.toLongOrNull()

fun JsonObject.bool(key: String): Boolean? = (this[key] as? JsonPrimitive)?.takeIf { !it.isString }?.content?.toBooleanStrictOrNull()

val JsonObject.type: String get() = str("t")!!

/** A reliable, ordered byte stream for one session (TCP socket, BLE link, in-memory pipe). */
interface ByteStream : Closeable {
    val input: InputStream
    val output: OutputStream
}

open class FrameStream(val stream: ByteStream) : Closeable {
    private val input = DataInputStream(stream.input)
    private val writeLock = Any()

    /** Reads one frame of at most [max] bytes. The body buffer grows with the bytes actually received. */
    open fun readFrame(max: Int = MAX_FRAME): ByteArray {
        val length = try {
            input.readInt()
        } catch (e: EOFException) {
            throw ProtocolError("closed", "stream closed")
        }
        if (length <= 0 || length > max) throw ProtocolError("bad_frame", "frame length ${length.toUInt()}")
        var body = ByteArray(minOf(length, READ_CHUNK))
        var n = 0
        while (n < length) {
            if (n == body.size) body = body.copyOf(minOf(length, body.size * 2))
            val r = input.read(body, n, body.size - n)
            if (r < 0) throw ProtocolError("closed", "stream closed mid-frame")
            n += r
        }
        return body
    }

    open fun writeFrame(body: ByteArray) {
        if (body.isEmpty() || body.size > MAX_FRAME) throw ProtocolError("bad_frame", "frame length ${body.size}")
        val frame = ByteBuffer.allocate(4 + body.size).putInt(body.size).put(body).array()
        synchronized(writeLock) {
            stream.output.write(frame)
            stream.output.flush()
        }
    }

    override fun close() = stream.close()

    private companion object {
        const val READ_CHUNK = 16 * 1024
    }
}

data class Peer(val id: String, val name: String, val platform: String, val static: ByteArray) {
    override fun equals(other: Any?) = other is Peer && other.id == id && other.static.contentEquals(static) &&
        other.name == name && other.platform == platform

    override fun hashCode() = id.hashCode()
}

/** Encrypted, authenticated message channel produced by a completed handshake. */
class SecureChannel(
    val frames: FrameStream,
    val peer: Peer,
    val sas: String,
    sendKey: ByteArray,
    recvKey: ByteArray,
) : Closeable {
    private val tx = CipherState(sendKey)
    private val rx = CipherState(recvKey)
    private val sendLock = Any()

    fun send(msg: JsonObject) {
        // Encrypt and write under one lock so record order on the wire matches counter order.
        synchronized(sendLock) { frames.writeFrame(tx.encrypt(encode(msg))) }
    }

    fun recv(): JsonObject = decode(rx.decrypt(frames.readFrame()))

    override fun close() = frames.close()
}

object Handshake {
    private val random = SecureRandom()

    fun helloBody(role: String, identity: KeyPair, eph: KeyPair, nonce: ByteArray, name: String, platform: String): ByteArray =
        encode(
            buildJsonObject {
                put("t", "hello")
                put("v", VERSION)
                put("role", role)
                put("id", deviceId(identity.public))
                put("name", name)
                put("platform", platform)
                put("static", b64e(identity.public))
                put("eph", b64e(eph.public))
                put("nonce", b64e(nonce))
            },
        )

    private fun isVersion1(el: JsonElement?) = el is JsonPrimitive && !el.isString && el.content == VERSION.toString()

    /** Returns the peer and its ephemeral public key. */
    fun parseHello(body: ByteArray, expectedRole: String): Pair<Peer, ByteArray> {
        val msg = decode(body)
        if (msg.type == "error") throw ProtocolError(msg.str("code") ?: "error", msg.str("message") ?: "")
        if (msg.type != "hello" || !isVersion1(msg["v"]) || msg.str("role") != expectedRole) {
            throw ProtocolError("bad_hello", "expected $expectedRole hello v$VERSION")
        }
        val static: ByteArray
        val eph: ByteArray
        val nonce: ByteArray
        try {
            static = b64d(msg.str("static") ?: throw IllegalArgumentException("missing static"))
            eph = b64d(msg.str("eph") ?: throw IllegalArgumentException("missing eph"))
            nonce = b64d(msg.str("nonce") ?: throw IllegalArgumentException("missing nonce"))
            validatePublicKey(static)
            validatePublicKey(eph)
        } catch (e: IllegalArgumentException) {
            throw ProtocolError("bad_hello", e.message ?: "invalid key")
        }
        val name = msg.str("name") ?: throw ProtocolError("bad_hello", "missing name")
        val platform = msg.str("platform") ?: throw ProtocolError("bad_hello", "missing platform")
        val id = msg.str("id") ?: throw ProtocolError("bad_hello", "missing id")
        if (nonce.size != 16) throw ProtocolError("bad_hello", "nonce must be 16 bytes")
        if (id != deviceId(static)) throw ProtocolError("bad_hello", "id does not match static key")
        return Peer(id, name, platform, static) to eph
    }

    private fun freshNonce() = ByteArray(16).also(random::nextBytes)

    fun initiate(
        frames: FrameStream,
        identity: KeyPair,
        name: String,
        platform: String,
        eph: KeyPair = KeyPair.generate(),
        nonce: ByteArray = freshNonce(),
    ): SecureChannel {
        val helloI = helloBody("initiator", identity, eph, nonce, name, platform)
        val commit = encode(
            buildJsonObject {
                put("t", "commit")
                put("v", VERSION)
                put("c", b64e(sha256(helloI)))
            },
        )
        frames.writeFrame(commit)
        val helloR = frames.readFrame(MAX_HANDSHAKE_FRAME)
        val (peer, peerEph) = parseHello(helloR, "responder")
        frames.writeFrame(helloI)
        val th = transcriptHash(commit, helloR, helloI)
        val (kI2r, kR2i) = sessionKeys(th, eph.dh(peerEph), identity.dh(peer.static))
        return SecureChannel(frames, peer, sasCode(th), sendKey = kI2r, recvKey = kR2i)
    }

    /**
     * Runs the responder side. On a protocol failure a plaintext `error` is sent when possible
     * and [ProtocolError] is thrown; the caller closes the stream.
     */
    fun respond(
        frames: FrameStream,
        identity: KeyPair,
        name: String,
        platform: String,
        eph: KeyPair = KeyPair.generate(),
        nonce: ByteArray = freshNonce(),
    ): SecureChannel {
        try {
            val commit = frames.readFrame(MAX_HANDSHAKE_FRAME)
            val msg = decode(commit)
            if (msg.type != "commit") throw ProtocolError("bad_commit", "expected commit")
            if (!isVersion1(msg["v"])) {
                throw ProtocolError("unsupported_version", "peer offered v${msg["v"]}")
            }
            val commitment = try {
                b64d(msg.str("c") ?: throw IllegalArgumentException("missing c"))
            } catch (e: IllegalArgumentException) {
                throw ProtocolError("bad_commit", e.message ?: "bad commitment")
            }
            val helloR = helloBody("responder", identity, eph, nonce, name, platform)
            frames.writeFrame(helloR)
            val helloI = frames.readFrame(MAX_HANDSHAKE_FRAME)
            // Hash the exact received bytes, never a re-serialization.
            if (!sha256(helloI).contentEquals(commitment)) throw ProtocolError("bad_commit", "hello does not match commitment")
            val (peer, peerEph) = parseHello(helloI, "initiator")
            val th = transcriptHash(commit, helloR, helloI)
            val (kI2r, kR2i) = sessionKeys(th, eph.dh(peerEph), identity.dh(peer.static))
            return SecureChannel(frames, peer, sasCode(th), sendKey = kR2i, recvKey = kI2r)
        } catch (e: ProtocolError) {
            if (e.code != "closed") {
                runCatching {
                    frames.writeFrame(
                        encode(
                            buildJsonObject {
                                put("t", "error")
                                put("code", e.code)
                                put("message", e.message)
                                if (e.code == "unsupported_version") put("supported", JsonArray(listOf(JsonPrimitive(VERSION))))
                            },
                        ),
                    )
                }
            }
            throw e
        }
    }
}
