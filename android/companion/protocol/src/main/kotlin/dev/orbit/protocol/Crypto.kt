package dev.orbit.protocol

import java.math.BigInteger
import java.nio.ByteBuffer
import java.security.AlgorithmParameters
import java.security.KeyFactory
import java.security.KeyPairGenerator
import java.security.MessageDigest
import java.security.PrivateKey
import java.security.PublicKey
import java.security.interfaces.ECPrivateKey
import java.security.interfaces.ECPublicKey
import java.security.spec.ECGenParameterSpec
import java.security.spec.ECParameterSpec
import java.security.spec.ECPoint
import java.security.spec.ECPrivateKeySpec
import java.security.spec.ECPublicKeySpec
import java.util.Base64
import javax.crypto.Cipher
import javax.crypto.KeyAgreement
import javax.crypto.Mac
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec

internal val TRANSCRIPT_LABEL = "orbit/1 transcript".toByteArray(Charsets.US_ASCII)
internal val KEYS_INFO = "orbit/1 session keys".toByteArray(Charsets.US_ASCII)
internal val SAS_LABEL = "orbit/1 sas".toByteArray(Charsets.US_ASCII)
const val TAG_LEN = 16

fun sha256(vararg parts: ByteArray): ByteArray {
    val md = MessageDigest.getInstance("SHA-256")
    parts.forEach(md::update)
    return md.digest()
}

fun b64e(data: ByteArray): String = Base64.getEncoder().encodeToString(data)

/** Strict standard-alphabet, padded base64; throws IllegalArgumentException otherwise. */
fun b64d(text: String): ByteArray = Base64.getDecoder().decode(text)

fun ByteArray.toHex(): String = joinToString("") { "%02x".format(it) }

fun hexToBytes(hex: String): ByteArray {
    require(hex.length % 2 == 0) { "odd hex length" }
    return ByteArray(hex.length / 2) { hex.substring(it * 2, it * 2 + 2).toInt(16).toByte() }
}

fun deviceId(staticPublic: ByteArray): String = sha256(staticPublic).copyOf(16).toHex()

/** NIST P-256 curve arithmetic needed beyond what JCA exposes (point validation, scalar -> public). */
internal object P256 {
    val p = BigInteger("ffffffff00000001000000000000000000000000ffffffffffffffffffffffff", 16)
    val a: BigInteger = p - BigInteger.valueOf(3)
    val b = BigInteger("5ac635d8aa3a93e7b3ebbd55769886bc651d06b0cc53b0f63bce3c3e27d2604b", 16)
    val n = BigInteger("ffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551", 16)
    private val gx = BigInteger("6b17d1f2e12c4247f8bce6e563a440f277037d812deb33a0f4a13945d898c296", 16)
    private val gy = BigInteger("4fe342e2fe1a7f9b8ee7eb4a7c0f9e162bce33576b315ececbb6406837bf51f5", 16)

    val params: ECParameterSpec by lazy {
        val ap = AlgorithmParameters.getInstance("EC")
        ap.init(ECGenParameterSpec("secp256r1"))
        ap.getParameterSpec(ECParameterSpec::class.java)
    }

    fun isOnCurve(x: BigInteger, y: BigInteger): Boolean {
        if (x.signum() < 0 || x >= p || y.signum() < 0 || y >= p) return false
        return y.modPow(BigInteger.TWO, p) == (x.pow(3) + a * x + b).mod(p)
    }

    /** Affine double-and-add; only used to derive public keys from injected test scalars. */
    fun multiplyG(k: BigInteger): Pair<BigInteger, BigInteger> {
        var rx: BigInteger? = null
        var ry: BigInteger? = null
        var qx = gx
        var qy = gy
        for (i in 0 until k.bitLength()) {
            if (k.testBit(i)) {
                if (rx == null) { rx = qx; ry = qy } else { val s = add(rx, ry!!, qx, qy); rx = s.first; ry = s.second }
            }
            val d = add(qx, qy, qx, qy); qx = d.first; qy = d.second
        }
        return rx!! to ry!!
    }

    private fun add(x1: BigInteger, y1: BigInteger, x2: BigInteger, y2: BigInteger): Pair<BigInteger, BigInteger> {
        val l = if (x1 == x2 && y1 == y2) {
            (BigInteger.valueOf(3) * x1 * x1 + a) * (BigInteger.TWO * y1).modInverse(p)
        } else {
            (y2 - y1) * (x2 - x1).modInverse(p)
        }.mod(p)
        val x3 = (l * l - x1 - x2).mod(p)
        return x3 to (l * (x1 - x3) - y1).mod(p)
    }

    fun encode(x: BigInteger, y: BigInteger): ByteArray = byteArrayOf(4) + fixed32(x) + fixed32(y)

    fun fixed32(v: BigInteger): ByteArray {
        val raw = v.toByteArray()
        return when {
            raw.size == 32 -> raw
            raw.size > 32 -> raw.copyOfRange(raw.size - 32, raw.size)
            else -> ByteArray(32 - raw.size) + raw
        }
    }

    /** Decodes and validates a 65-byte uncompressed SEC1 point. */
    fun decodePublic(data: ByteArray): PublicKey {
        require(data.size == 65 && data[0] == 4.toByte()) { "public key must be a 65-byte uncompressed P-256 point" }
        val x = BigInteger(1, data.copyOfRange(1, 33))
        val y = BigInteger(1, data.copyOfRange(33, 65))
        require(isOnCurve(x, y)) { "point is not on P-256" }
        return KeyFactory.getInstance("EC").generatePublic(ECPublicKeySpec(ECPoint(x, y), params))
    }
}

/** Validates that [data] is a usable P-256 public key; throws IllegalArgumentException otherwise. */
fun validatePublicKey(data: ByteArray) {
    P256.decodePublic(data)
}

class KeyPair private constructor(private val private: PrivateKey, val public: ByteArray) {
    /** Big-endian 32-byte private scalar, for persistence. */
    val scalar: ByteArray get() = P256.fixed32((private as ECPrivateKey).s)

    fun dh(peerPublic: ByteArray): ByteArray {
        val ka = KeyAgreement.getInstance("ECDH")
        ka.init(private)
        ka.doPhase(P256.decodePublic(peerPublic), true)
        return ka.generateSecret()
    }

    companion object {
        fun generate(): KeyPair {
            val gen = KeyPairGenerator.getInstance("EC")
            gen.initialize(ECGenParameterSpec("secp256r1"))
            val kp = gen.generateKeyPair()
            val w = (kp.public as ECPublicKey).w
            return KeyPair(kp.private, P256.encode(w.affineX, w.affineY))
        }

        fun fromScalar(scalar: ByteArray): KeyPair {
            val s = BigInteger(1, scalar)
            require(s.signum() > 0 && s < P256.n) { "scalar out of range" }
            val (x, y) = P256.multiplyG(s)
            return fromStored(scalar, P256.encode(x, y))
        }

        /** Restores a key pair persisted as ([scalar], [public]) without recomputing the public point. */
        fun fromStored(scalar: ByteArray, public: ByteArray): KeyPair {
            P256.decodePublic(public)
            val priv = KeyFactory.getInstance("EC").generatePrivate(ECPrivateKeySpec(BigInteger(1, scalar), P256.params))
            return KeyPair(priv, public.copyOf())
        }
    }
}

private fun lp(data: ByteArray): ByteArray = ByteBuffer.allocate(4).putInt(data.size).array() + data

fun transcriptHash(commitBody: ByteArray, helloR: ByteArray, helloI: ByteArray): ByteArray =
    sha256(TRANSCRIPT_LABEL, lp(commitBody), lp(helloR), lp(helloI))

fun hkdfSha256(salt: ByteArray, ikm: ByteArray, info: ByteArray, length: Int): ByteArray {
    val mac = Mac.getInstance("HmacSHA256")
    mac.init(SecretKeySpec(salt.takeIf { it.isNotEmpty() } ?: ByteArray(32), "HmacSHA256"))
    val prk = mac.doFinal(ikm)
    mac.init(SecretKeySpec(prk, "HmacSHA256"))
    val out = java.io.ByteArrayOutputStream()
    var t = ByteArray(0)
    var i = 1
    while (out.size() < length) {
        mac.update(t)
        mac.update(info)
        mac.update(i.toByte())
        t = mac.doFinal()
        out.write(t)
        i++
    }
    return out.toByteArray().copyOf(length)
}

/** Returns (k_i2r, k_r2i). */
fun sessionKeys(th: ByteArray, dhEe: ByteArray, dhSs: ByteArray): Pair<ByteArray, ByteArray> {
    val okm = hkdfSha256(th, dhEe + dhSs, KEYS_INFO, 64)
    return okm.copyOfRange(0, 32) to okm.copyOfRange(32, 64)
}

fun sasCode(th: ByteArray): String {
    val v = ByteBuffer.wrap(sha256(SAS_LABEL, th), 0, 4).int.toLong() and 0xffffffffL
    return "%06d".format(v % 1_000_000)
}

/** One direction of an encrypted session: AES-256-GCM, nonce = 4 zero bytes || uint64_be(counter). */
class CipherState(key: ByteArray) {
    private val key = SecretKeySpec(key.copyOf(), "AES")
    private val cipher = Cipher.getInstance("AES/GCM/NoPadding")

    /** Next counter, interpreted as unsigned 64-bit. */
    var counter: Long = 0
        private set

    private fun nextNonce(): ByteArray {
        check(counter != -1L) { "nonce counter exhausted" }
        val n = ByteBuffer.allocate(12).putInt(0).putLong(counter).array()
        counter++
        return n
    }

    fun encrypt(plaintext: ByteArray): ByteArray {
        cipher.init(Cipher.ENCRYPT_MODE, key, GCMParameterSpec(TAG_LEN * 8, nextNonce()))
        return cipher.doFinal(plaintext)
    }

    /** Throws [ProtocolError] `decrypt_failed` if the record does not authenticate. */
    fun decrypt(record: ByteArray): ByteArray {
        cipher.init(Cipher.DECRYPT_MODE, key, GCMParameterSpec(TAG_LEN * 8, nextNonce()))
        try {
            return cipher.doFinal(record)
        } catch (e: java.security.GeneralSecurityException) {
            throw ProtocolError("decrypt_failed", "record failed authentication")
        }
    }
}
