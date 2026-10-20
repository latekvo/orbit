package dev.orbit.companion

import android.bluetooth.BluetoothGatt
import java.io.FilterInputStream
import java.io.FilterOutputStream
import java.io.InputStream
import java.io.OutputStream
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicLong

/** What a central's BLE session runs over (spec 1.2, 1.2.1). */
enum class LinkKind(val label: String) { GATT("gatt"), CHANNEL("l2cap channel") }

interface BleSessionLink {
    val kind: LinkKind

    /** Ends the session; [dropConnection] also closes the LE connection. Idempotent. */
    fun close(reason: String, dropConnection: Boolean)
}

/**
 * Each central's live session link, by address. A central runs one session, over the channel or
 * over RX/TX (spec 1.2.1); a newer one replaces the older, which closes without dropping the LE
 * connection the newer one runs on.
 */
class BleLinks<L : BleSessionLink> {
    private val links = ConcurrentHashMap<String, L>()

    operator fun get(address: String): L? = links[address]

    fun open(address: String, link: L) {
        links.put(address, link)?.close("replaced by ${link.kind.label}", dropConnection = false)
    }

    /** Notifications disabled: ends a GATT session only, a channel session does not use TX. */
    fun unsubscribed(address: String) {
        val link = links[address]?.takeIf { it.kind == LinkKind.GATT } ?: return
        if (links.remove(address, link)) link.close("notifications disabled", dropConnection = true)
    }

    fun disconnected(address: String) {
        links.remove(address)?.close("disconnected", dropConnection = false)
    }

    fun remove(address: String, link: L) = links.remove(address, link)

    fun closeAll(reason: String) {
        links.forEach { (address, link) -> if (links.remove(address, link)) link.close(reason, dropConnection = true) }
    }
}

/** Bytes and time over one session link, for the field log. Thread-safe. */
class LinkMeter(private val elapsedMs: () -> Long) {
    private val start = elapsedMs()
    private val sent = AtomicLong()
    private val received = AtomicLong()

    fun input(stream: InputStream): InputStream = object : FilterInputStream(stream) {
        override fun read(): Int = super.read().also { if (it >= 0) received.incrementAndGet() }
        override fun read(b: ByteArray, off: Int, len: Int): Int = super.read(b, off, len).also { if (it > 0) received.addAndGet(it.toLong()) }
    }

    fun output(stream: OutputStream): OutputStream = object : FilterOutputStream(stream) {
        override fun write(b: Int) = write(byteArrayOf(b.toByte()), 0, 1)
        override fun write(b: ByteArray, off: Int, len: Int) {
            out.write(b, off, len)
            sent.addAndGet(len.toLong())
        }
    }

    fun summary(): String {
        val ms = elapsedMs() - start
        val out = sent.get()
        return "sent $out B, received ${received.get()} B in ${"%.1f".format(java.util.Locale.US, ms / 1000.0)} s " +
            "(${out * 1000 / ms.coerceAtLeast(1)} B/s out)"
    }
}

/** Spec 1.2 maximum. */
const val MAX_NOTIFICATION = 512

/**
 * TX chunk for an ATT MTU (23 until the central negotiates one). Android takes about three
 * connection intervals per notification whatever its size (it must wait for onNotificationSent),
 * so the bytes each one carries set the throughput; the controller fragments a PDU longer than
 * one link-layer packet.
 */
fun notificationChunk(mtu: Int?): Int = ((mtu ?: 23) - 3).coerceIn(20, MAX_NOTIFICATION)

/** PSM characteristic value (spec 1.2.1): u16 little-endian. */
fun psmValue(psm: Int): ByteArray {
    require(psm in 0..0xffff) { "psm $psm" }
    return byteArrayOf(psm.toByte(), (psm shr 8).toByte())
}

/**
 * ATT "Unlikely Error". GATT_FAILURE (0x101) does not fit the one-byte ATT error code; this tells
 * the central the PSM is unavailable, so it falls back to RX/TX.
 */
const val ATT_UNLIKELY_ERROR = 0x0E

/** (status, value) answering a read at [offset] of [value]; a null value is not available. */
fun readResponse(value: ByteArray?, offset: Int): Pair<Int, ByteArray?> = when {
    value == null -> ATT_UNLIKELY_ERROR to null
    offset < 0 || offset > value.size -> BluetoothGatt.GATT_INVALID_OFFSET to null
    else -> BluetoothGatt.GATT_SUCCESS to value.copyOfRange(offset, value.size)
}
