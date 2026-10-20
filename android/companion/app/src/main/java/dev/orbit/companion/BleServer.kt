package dev.orbit.companion

import android.Manifest
import android.annotation.SuppressLint
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.bluetooth.BluetoothGattServer
import android.bluetooth.BluetoothGattServerCallback
import android.bluetooth.BluetoothGattService
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothProfile
import android.bluetooth.BluetoothServerSocket
import android.bluetooth.BluetoothSocket
import android.bluetooth.le.AdvertiseCallback
import android.bluetooth.le.AdvertiseData
import android.bluetooth.le.AdvertiseSettings
import android.bluetooth.le.BluetoothLeAdvertiser
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import android.os.ParcelUuid
import android.os.SystemClock
import android.util.Log
import dev.orbit.protocol.ByteStream
import dev.orbit.protocol.ResponderSession
import dev.orbit.protocol.hexToBytes
import kotlinx.coroutines.flow.update
import java.io.ByteArrayOutputStream
import java.io.IOException
import java.io.InputStream
import java.io.OutputStream
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.Semaphore
import java.util.concurrent.TimeUnit

/**
 * BLE GATT peripheral transport (spec 1.2), with sessions over an L2CAP channel when the central
 * opens one (1.2.1). Every failure degrades to a status string, never a crash.
 */
// start() checks the permissions; callbacks only arrive after it succeeded, and revoking a runtime
// permission kills the process.
@SuppressLint("MissingPermission")
class BleServer(private val context: Context, private val orbit: Orbit) {
    @Volatile var running = false
        private set

    private var gatt: BluetoothGattServer? = null
    private var advertiser: BluetoothLeAdvertiser? = null
    private val links = BleLinks<Link>()
    @Volatile private var channelServer: BluetoothServerSocket? = null
    /** PSM characteristic value while [channelServer] listens. */
    @Volatile private var psm: ByteArray? = null
    private val mtus = ConcurrentHashMap<String, Int>()
    private val preparedWrites = ConcurrentHashMap<String, ByteArrayOutputStream>()
    private val notifyLock = Any() // the TX characteristic is shared by every link
    private lateinit var tx: BluetoothGattCharacteristic

    private fun hasPermissions(): Boolean = Build.VERSION.SDK_INT < Build.VERSION_CODES.S || listOf(
        Manifest.permission.BLUETOOTH_CONNECT,
        Manifest.permission.BLUETOOTH_ADVERTISE,
    ).all { context.checkSelfPermission(it) == PackageManager.PERMISSION_GRANTED }

    /** Starts the GATT server and advertising; returns a human-readable status. */
    fun start(): String {
        if (!context.packageManager.hasSystemFeature(PackageManager.FEATURE_BLUETOOTH_LE)) return "unavailable: no BLE hardware"
        val manager = context.getSystemService(BluetoothManager::class.java) ?: return "unavailable: no Bluetooth"
        val adapter = manager.adapter ?: return "unavailable: no Bluetooth"
        if (!hasPermissions()) return "permission missing"
        return try {
            if (!adapter.isEnabled) return "bluetooth off"
            val adv = adapter.bluetoothLeAdvertiser ?: return "unavailable: peripheral mode not supported"
            val server = manager.openGattServer(context, callback) ?: return "unavailable: could not open GATT server"
            EventLog.log("gatt server opened")
            gatt = server
            advertiser = adv
            tx = BluetoothGattCharacteristic(
                TX_UUID, BluetoothGattCharacteristic.PROPERTY_NOTIFY, BluetoothGattCharacteristic.PERMISSION_READ,
            ).apply {
                addDescriptor(
                    BluetoothGattDescriptor(
                        CCCD_UUID, BluetoothGattDescriptor.PERMISSION_READ or BluetoothGattDescriptor.PERMISSION_WRITE,
                    ),
                )
            }
            val rx = BluetoothGattCharacteristic(
                RX_UUID, BluetoothGattCharacteristic.PROPERTY_WRITE, BluetoothGattCharacteristic.PERMISSION_WRITE,
            )
            val channels = listenForChannels(adapter)
            val service = BluetoothGattService(SERVICE_UUID, BluetoothGattService.SERVICE_TYPE_PRIMARY).apply {
                addCharacteristic(rx)
                addCharacteristic(tx)
                // Without a channel server the characteristic is absent and centrals use RX/TX.
                if (channels) {
                    addCharacteristic(
                        BluetoothGattCharacteristic(
                            PSM_UUID, BluetoothGattCharacteristic.PROPERTY_READ, BluetoothGattCharacteristic.PERMISSION_READ,
                        ),
                    )
                }
            }
            if (!server.addService(service)) {
                stop()
                return "unavailable: could not add GATT service"
            }
            running = true
            "starting"
        } catch (e: SecurityException) {
            stop()
            "permission missing"
        } catch (e: RuntimeException) {
            Log.w(TAG, "ble start failed", e)
            stop()
            "unavailable: ${e.message}"
        }
    }

    /** Opens the L2CAP channel server (a new PSM each time) and its accept thread; false if it cannot listen. */
    private fun listenForChannels(adapter: BluetoothAdapter): Boolean {
        val server = try {
            adapter.listenUsingInsecureL2capChannel()
        } catch (e: Exception) {
            EventLog.log("ble l2cap listen failed: $e; sessions use gatt only")
            return false
        }
        channelServer = server
        psm = psmValue(server.psm)
        EventLog.log("ble l2cap listening (psm ${server.psm})")
        // Nothing accepts once the thread is gone, so stop listening and let centrals use RX/TX.
        daemonThreads("orbit-l2cap-accept") {
            if (closeChannelServer(server)) EventLog.log("ble l2cap accept thread died: $it; channel server closed")
        }.newThread { acceptChannels(server) }.start()
        return true
    }

    private fun acceptChannels(server: BluetoothServerSocket) {
        while (true) {
            val socket = try {
                server.accept()
            } catch (e: IOException) {
                // The listening socket closed or broke (e.g. Bluetooth turning off); PSM reads now fail.
                if (closeChannelServer(server)) EventLog.log("ble l2cap accept failed: $e; channel server closed")
                return
            }
            val device = socket.remoteDevice
            EventLog.log("ble ${device.address} l2cap channel accepted (psm ${server.psm})")
            val link = try {
                ChannelLink(device, socket)
            } catch (e: IOException) {
                EventLog.log("ble ${device.address} l2cap channel unusable: $e")
                runCatching { socket.close() }
                continue
            }
            links.open(device.address, link)
            if (channelServer !== server) {
                link.close("channel server closed", dropConnection = false)
                return
            }
            link.session = orbit.startSession(link, "ble")
        }
    }

    /** Closes [server] unless already closed; true if this call closed it. */
    private fun closeChannelServer(server: BluetoothServerSocket): Boolean {
        synchronized(this) {
            if (channelServer !== server) return false
            channelServer = null
            psm = null
        }
        runCatching { server.close() }
        return true
    }

    private fun startAdvertising() {
        val adv = advertiser ?: return
        val settings = AdvertiseSettings.Builder()
            .setAdvertiseMode(AdvertiseSettings.ADVERTISE_MODE_BALANCED)
            .setTxPowerLevel(AdvertiseSettings.ADVERTISE_TX_POWER_MEDIUM)
            .setConnectable(true)
            .setTimeout(0)
            .build()
        val uuid = ParcelUuid(SERVICE_UUID)
        val data = AdvertiseData.Builder().addServiceUuid(uuid).setIncludeDeviceName(false).build()
        // Service data for a 128-bit UUID takes 26 of the 31 bytes; the name goes along only if it fits.
        val name = runCatching { context.getSystemService(BluetoothManager::class.java).adapter.name }.getOrNull().orEmpty()
        val scanResponse = AdvertiseData.Builder()
            .addServiceData(uuid, hexToBytes(orbit.deviceId.substring(0, 16)))
            .setIncludeDeviceName(name.isNotEmpty() && name.toByteArray().size <= 3)
            .build()
        try {
            adv.startAdvertising(settings, data, scanResponse, advertiseCallback)
        } catch (e: SecurityException) {
            status("permission missing")
        } catch (e: RuntimeException) {
            status("advertising failed: ${e.message}")
        }
    }

    private val advertiseCallback = object : AdvertiseCallback() {
        override fun onStartSuccess(settingsInEffect: AdvertiseSettings) {
            EventLog.log("ble advertising started")
            status("advertising")
        }

        override fun onStartFailure(errorCode: Int) {
            EventLog.log("ble advertising failed ($errorCode)")
            status("advertising failed ($errorCode)")
        }
    }

    private fun status(s: String) {
        Log.i(TAG, "ble: $s")
        orbit.transports.update { it.copy(ble = s) }
    }

    fun stop() {
        running = false
        channelServer?.let { if (closeChannelServer(it)) EventLog.log("ble l2cap channel server closed") }
        links.closeAll("ble server stopped")
        runCatching { advertiser?.stopAdvertising(advertiseCallback) }
        gatt?.let {
            runCatching { it.close() }
            EventLog.log("gatt server closed")
        }
        gatt = null
        advertiser = null
    }

    private val callback = object : BluetoothGattServerCallback() {
        override fun onServiceAdded(status: Int, service: BluetoothGattService) {
            EventLog.log("gatt service added (status $status)")
            if (status == BluetoothGatt.GATT_SUCCESS) startAdvertising() else status("GATT service failed ($status)")
        }

        override fun onConnectionStateChange(device: BluetoothDevice, status: Int, newState: Int) {
            val state = when (newState) {
                BluetoothProfile.STATE_CONNECTED -> "connected"
                BluetoothProfile.STATE_DISCONNECTED -> "disconnected"
                else -> "state $newState"
            }
            EventLog.log("ble ${device.address} $state (status $status)")
            if (newState == BluetoothProfile.STATE_DISCONNECTED) {
                links.disconnected(device.address)
                mtus.remove(device.address)
                preparedWrites.remove(device.address)
            }
        }

        override fun onMtuChanged(device: BluetoothDevice, mtu: Int) {
            EventLog.log("ble ${device.address} mtu $mtu")
            mtus[device.address] = mtu
        }

        override fun onDescriptorReadRequest(device: BluetoothDevice, requestId: Int, offset: Int, descriptor: BluetoothGattDescriptor) {
            val value = if (links[device.address]?.kind == LinkKind.GATT) {
                BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE
            } else {
                BluetoothGattDescriptor.DISABLE_NOTIFICATION_VALUE
            }
            respond(device, requestId, BluetoothGatt.GATT_SUCCESS, offset, value)
        }

        override fun onDescriptorWriteRequest(
            device: BluetoothDevice, requestId: Int, descriptor: BluetoothGattDescriptor,
            preparedWrite: Boolean, responseNeeded: Boolean, offset: Int, value: ByteArray?,
        ) {
            if (descriptor.uuid == CCCD_UUID && descriptor.characteristic.uuid == TX_UUID) {
                val enable = value != null && value.isNotEmpty() && (value[0].toInt() and 1) != 0
                EventLog.log("ble ${device.address} notifications ${if (enable) "enabled" else "disabled"}")
                if (enable) {
                    // A new subscription starts a new session; any previous one of this central ends.
                    val link = GattLink(device)
                    links.open(device.address, link)
                    link.session = orbit.startSession(link, "ble")
                } else {
                    links.unsubscribed(device.address)
                }
            }
            if (responseNeeded) respond(device, requestId, BluetoothGatt.GATT_SUCCESS, offset, null)
        }

        override fun onCharacteristicWriteRequest(
            device: BluetoothDevice, requestId: Int, characteristic: BluetoothGattCharacteristic,
            preparedWrite: Boolean, responseNeeded: Boolean, offset: Int, value: ByteArray?,
        ) {
            var status = BluetoothGatt.GATT_SUCCESS
            if (characteristic.uuid != RX_UUID) {
                status = BluetoothGatt.GATT_WRITE_NOT_PERMITTED
            } else if (preparedWrite) {
                preparedWrites.getOrPut(device.address) { ByteArrayOutputStream() }.write(value ?: ByteArray(0))
            } else {
                val link = links[device.address] as? GattLink
                if (link == null) status = BluetoothGatt.GATT_FAILURE else link.feed(value ?: ByteArray(0))
            }
            if (responseNeeded) respond(device, requestId, status, offset, value)
        }

        override fun onExecuteWrite(device: BluetoothDevice, requestId: Int, execute: Boolean) {
            val buffered = preparedWrites.remove(device.address)
            if (execute && buffered != null) (links[device.address] as? GattLink)?.feed(buffered.toByteArray())
            respond(device, requestId, BluetoothGatt.GATT_SUCCESS, 0, null)
        }

        override fun onNotificationSent(device: BluetoothDevice, status: Int) {
            (links[device.address] as? GattLink)?.onNotificationSent(status)
        }

        override fun onCharacteristicReadRequest(device: BluetoothDevice, requestId: Int, offset: Int, characteristic: BluetoothGattCharacteristic) {
            val (status, value) = if (characteristic.uuid == PSM_UUID) {
                readResponse(psm, offset)
            } else {
                BluetoothGatt.GATT_READ_NOT_PERMITTED to null
            }
            respond(device, requestId, status, offset, value)
        }
    }

    private fun respond(device: BluetoothDevice, requestId: Int, status: Int, offset: Int, value: ByteArray?) {
        try {
            gatt?.sendResponse(device, requestId, status, offset, value)
        } catch (e: SecurityException) {
            Log.w(TAG, "ble sendResponse denied", e)
        }
    }

    /** One central's session byte stream; logs its throughput when it ends. */
    private abstract inner class Link(val device: BluetoothDevice, final override val kind: LinkKind) : ByteStream, BleSessionLink {
        protected val meter = LinkMeter(SystemClock::elapsedRealtime)
        @Volatile var session: ResponderSession? = null
        @Volatile protected var closed = false

        /** Unblocks the link's reads and writes. */
        protected abstract fun release()

        /** The session closing its stream; its close reason is set by then. */
        final override fun close() = close(session?.closeReason ?: "closed", dropConnection = true)

        final override fun close(reason: String, dropConnection: Boolean) {
            synchronized(this) {
                if (closed) return
                closed = true
            }
            release()
            links.remove(device.address, this)
            EventLog.log("ble ${device.address} ${kind.label} session ended: $reason; ${meter.summary()}")
            if (!dropConnection) return
            // Ending the session drops the LE connection, so the central sees the session end.
            try {
                gatt?.cancelConnection(device)
            } catch (_: SecurityException) {
            }
        }
    }

    /** A session over an accepted L2CAP channel (spec 1.2.1). */
    private inner class ChannelLink(device: BluetoothDevice, private val socket: BluetoothSocket) : Link(device, LinkKind.CHANNEL) {
        override val input: InputStream = meter.input(socket.inputStream)
        override val output: OutputStream = meter.output(socket.outputStream)

        override fun release() {
            runCatching { socket.close() }
        }
    }

    /** A session over RX writes in and TX notifications out. */
    private inner class GattLink(device: BluetoothDevice) : Link(device, LinkKind.GATT) {
        private val lock = Object()
        private val inbound = java.util.ArrayDeque<ByteArray>()
        private var head = 0
        private val sent = Semaphore(0)
        @Volatile private var sentStatus = BluetoothGatt.GATT_SUCCESS

        fun feed(data: ByteArray) {
            if (data.isEmpty()) return
            synchronized(lock) {
                inbound.addLast(data)
                lock.notifyAll()
            }
        }

        fun onNotificationSent(status: Int) {
            sentStatus = status
            sent.release()
        }

        override val input: InputStream = meter.input(object : InputStream() {
            override fun read(): Int {
                val b = ByteArray(1)
                return if (read(b, 0, 1) < 0) -1 else b[0].toInt() and 0xff
            }

            override fun read(b: ByteArray, off: Int, len: Int): Int {
                if (len == 0) return 0
                synchronized(lock) {
                    while (inbound.isEmpty()) {
                        if (closed) return -1
                        lock.wait()
                    }
                    val chunk = inbound.first()
                    val n = minOf(len, chunk.size - head)
                    System.arraycopy(chunk, head, b, off, n)
                    head += n
                    if (head == chunk.size) {
                        inbound.removeFirst()
                        head = 0
                    }
                    return n
                }
            }
        })

        override val output: OutputStream = meter.output(object : OutputStream() {
            override fun write(b: Int) = write(byteArrayOf(b.toByte()), 0, 1)

            override fun write(b: ByteArray, off: Int, len: Int) {
                val chunkSize = notificationChunk(mtus[device.address])
                var pos = off
                while (pos < off + len) {
                    val n = minOf(chunkSize, off + len - pos)
                    sendChunk(b.copyOfRange(pos, pos + n))
                    pos += n
                }
            }
        })

        private fun sendChunk(chunk: ByteArray) {
            synchronized(notifyLock) {
                val start = SystemClock.elapsedRealtime()
                var busy = 0
                var lastCode = 0
                while (true) {
                    if (closed) throw IOException("ble link closed")
                    val server = gatt ?: throw IOException("gatt server stopped")
                    val code = try {
                        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                            server.notifyCharacteristicChanged(device, tx, false, chunk)
                        } else {
                            @Suppress("DEPRECATION")
                            tx.value = chunk
                            @Suppress("DEPRECATION")
                            if (server.notifyCharacteristicChanged(device, tx, false)) NOTIFY_OK else -1
                        }
                    } catch (e: SecurityException) {
                        throw IOException("bluetooth permission revoked", e)
                    }
                    if (code == NOTIFY_OK) break
                    busy++
                    lastCode = code
                    // The stack is busy (e.g. congested): retry briefly.
                    if (SystemClock.elapsedRealtime() - start > NOTIFY_TIMEOUT_MS) {
                        EventLog.log("ble ${device.address} notify not accepted for ${NOTIFY_TIMEOUT_MS} ms ($busy tries, last code $lastCode)")
                        throw IOException("notification not accepted ($lastCode)")
                    }
                    Thread.sleep(10)
                }
                if (busy > 0) {
                    EventLog.log("ble ${device.address} stack busy: notify accepted after $busy tries, ${SystemClock.elapsedRealtime() - start} ms (last code $lastCode)")
                }
                if (!sent.tryAcquire(NOTIFY_TIMEOUT_MS, TimeUnit.MILLISECONDS)) {
                    EventLog.log("ble ${device.address} onNotificationSent timeout (${NOTIFY_TIMEOUT_MS} ms)")
                    throw IOException("onNotificationSent timeout")
                }
                if (closed) throw IOException("ble link closed")
                if (sentStatus != BluetoothGatt.GATT_SUCCESS) {
                    EventLog.log("ble ${device.address} notification failed (status $sentStatus)")
                    throw IOException("notification failed ($sentStatus)")
                }
            }
        }

        override fun release() {
            synchronized(lock) { lock.notifyAll() }
            sent.release()
        }
    }

    companion object {
        val SERVICE_UUID: UUID = UUID.fromString("616dfa5c-069e-4839-8335-87edbeb2c1a5")
        val RX_UUID: UUID = UUID.fromString("616dfa5d-069e-4839-8335-87edbeb2c1a5")
        val TX_UUID: UUID = UUID.fromString("616dfa5e-069e-4839-8335-87edbeb2c1a5")
        val PSM_UUID: UUID = UUID.fromString("616dfa5f-069e-4839-8335-87edbeb2c1a5")
        val CCCD_UUID: UUID = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")
        private const val NOTIFY_TIMEOUT_MS = 5_000L
        /** BluetoothStatusCodes.SUCCESS (API 31). */
        private const val NOTIFY_OK = 0
    }
}
