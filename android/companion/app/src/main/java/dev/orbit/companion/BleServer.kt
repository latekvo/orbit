package dev.orbit.companion

import android.Manifest
import android.annotation.SuppressLint
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.bluetooth.BluetoothGattServer
import android.bluetooth.BluetoothGattServerCallback
import android.bluetooth.BluetoothGattService
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothProfile
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

/** BLE GATT peripheral transport (spec 1.2). Every failure degrades to a status string, never a crash. */
// start() checks the permissions; callbacks only arrive after it succeeded, and revoking a runtime
// permission kills the process.
@SuppressLint("MissingPermission")
class BleServer(private val context: Context, private val orbit: Orbit) {
    @Volatile var running = false
        private set

    private var gatt: BluetoothGattServer? = null
    private var advertiser: BluetoothLeAdvertiser? = null
    private val links = ConcurrentHashMap<String, BleLink>()
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
            val service = BluetoothGattService(SERVICE_UUID, BluetoothGattService.SERVICE_TYPE_PRIMARY).apply {
                addCharacteristic(rx)
                addCharacteristic(tx)
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
        links.values.forEach { it.close() }
        links.clear()
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
                links.remove(device.address)?.close()
                mtus.remove(device.address)
                preparedWrites.remove(device.address)
            }
        }

        override fun onMtuChanged(device: BluetoothDevice, mtu: Int) {
            EventLog.log("ble ${device.address} mtu $mtu")
            mtus[device.address] = mtu
        }

        override fun onDescriptorReadRequest(device: BluetoothDevice, requestId: Int, offset: Int, descriptor: BluetoothGattDescriptor) {
            val value = if (links.containsKey(device.address)) {
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
                // A new subscription starts a new session; any previous one on this link ends.
                links.remove(device.address)?.close()
                EventLog.log("ble ${device.address} notifications ${if (enable) "enabled" else "disabled"}")
                if (enable) {
                    val link = BleLink(device)
                    links[device.address] = link
                    orbit.startSession(link, "ble")
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
                val link = links[device.address]
                if (link == null) status = BluetoothGatt.GATT_FAILURE else link.feed(value ?: ByteArray(0))
            }
            if (responseNeeded) respond(device, requestId, status, offset, value)
        }

        override fun onExecuteWrite(device: BluetoothDevice, requestId: Int, execute: Boolean) {
            val buffered = preparedWrites.remove(device.address)
            if (execute && buffered != null) links[device.address]?.feed(buffered.toByteArray())
            respond(device, requestId, BluetoothGatt.GATT_SUCCESS, 0, null)
        }

        override fun onNotificationSent(device: BluetoothDevice, status: Int) {
            links[device.address]?.onNotificationSent(status)
        }
    }

    private fun respond(device: BluetoothDevice, requestId: Int, status: Int, offset: Int, value: ByteArray?) {
        try {
            gatt?.sendResponse(device, requestId, status, offset, value)
        } catch (e: SecurityException) {
            Log.w(TAG, "ble sendResponse denied", e)
        }
    }

    /** One central's session byte stream: RX writes in, TX notifications out. */
    private inner class BleLink(private val device: BluetoothDevice) : ByteStream {
        private val lock = Object()
        private val inbound = java.util.ArrayDeque<ByteArray>()
        private var head = 0
        @Volatile private var closed = false
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

        override val input: InputStream = object : InputStream() {
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
        }

        override val output: OutputStream = object : OutputStream() {
            override fun write(b: Int) = write(byteArrayOf(b.toByte()), 0, 1)

            override fun write(b: ByteArray, off: Int, len: Int) {
                val chunkSize = ((mtus[device.address] ?: 23) - 3).coerceIn(20, MAX_NOTIFICATION)
                var pos = off
                while (pos < off + len) {
                    val n = minOf(chunkSize, off + len - pos)
                    sendChunk(b.copyOfRange(pos, pos + n))
                    pos += n
                }
            }
        }

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

        override fun close() {
            if (closed) return
            closed = true
            synchronized(lock) { lock.notifyAll() }
            sent.release()
            links.remove(device.address, this)
            // Ending the session drops the link, so the central sees the session end.
            try {
                gatt?.cancelConnection(device)
            } catch (_: SecurityException) {
            }
        }
    }

    companion object {
        val SERVICE_UUID: UUID = UUID.fromString("616dfa5c-069e-4839-8335-87edbeb2c1a5")
        val RX_UUID: UUID = UUID.fromString("616dfa5d-069e-4839-8335-87edbeb2c1a5")
        val TX_UUID: UUID = UUID.fromString("616dfa5e-069e-4839-8335-87edbeb2c1a5")
        val CCCD_UUID: UUID = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")
        private const val NOTIFY_TIMEOUT_MS = 5_000L
        /** BluetoothStatusCodes.SUCCESS (API 31). */
        private const val NOTIFY_OK = 0
        /** 251-byte LE data PDU minus L2CAP and ATT headers: each notification fits one link-layer packet. */
        private const val MAX_NOTIFICATION = 244
    }
}
