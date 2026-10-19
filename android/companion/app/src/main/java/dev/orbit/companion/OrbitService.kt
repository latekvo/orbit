package dev.orbit.companion

import android.app.Notification
import android.app.PendingIntent
import android.app.Service
import android.bluetooth.BluetoothAdapter
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.ServiceInfo
import android.os.IBinder
import androidx.core.content.ContextCompat
import kotlinx.coroutines.flow.update

/** Foreground service hosting the TCP server, DNS-SD registration and the BLE peripheral. */
class OrbitService : Service() {
    private lateinit var orbit: Orbit
    private var tcp: TcpServer? = null
    private var nsd: NsdAdvertiser? = null
    private var ble: BleServer? = null
    private var foreground = false

    private val btReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            val state = intent.getIntExtra(BluetoothAdapter.EXTRA_STATE, -1)
            EventLog.log("bluetooth state $state (${BT_STATES[state] ?: "?"})")
            when (state) {
                BluetoothAdapter.STATE_ON -> startBle()
                BluetoothAdapter.STATE_TURNING_OFF, BluetoothAdapter.STATE_OFF -> stopBle("bluetooth off")
            }
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        orbit = Orbit.get(this)
        try {
            startForeground(NOTIFICATION_ID, buildNotification(), ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE)
        } catch (e: IllegalStateException) {
            // Android 12+ may refuse a background start, e.g. a sticky restart without a battery exemption.
            EventLog.log("foreground start refused: $e")
            stopSelf()
            return
        }
        foreground = true
        val server = TcpServer(orbit)
        val port = server.start()
        tcp = server
        orbit.transports.update { it.copy(serviceRunning = true, tcpPort = port) }
        nsd = NsdAdvertiser(this) { s -> orbit.transports.update { it.copy(nsd = s) } }.also {
            it.start(orbit.deviceName, port, orbit.deviceId)
        }
        // A protected system broadcast still reaches a not-exported receiver.
        ContextCompat.registerReceiver(
            this, btReceiver, IntentFilter(BluetoothAdapter.ACTION_STATE_CHANGED), ContextCompat.RECEIVER_NOT_EXPORTED,
        )
        startBle()
        EventLog.log("service created: tcp port $port")
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        // A null intent is the system restarting the sticky service after its process died.
        val source = if (intent == null) "system restart (sticky)" else intent.getStringExtra(EXTRA_SOURCE) ?: "unknown"
        EventLog.log("service start #$startId from $source" + (intent?.action?.let { ", action ${it.substringAfterLast('.')}" } ?: ""))
        if (!foreground) return START_NOT_STICKY
        when (intent?.action) {
            ACTION_ENSURE_BLE -> startBle()
            ACTION_RENAME -> nsd?.let { n ->
                n.stop()
                tcp?.port?.let { n.start(orbit.deviceName, it, orbit.deviceId) }
            }
        }
        return START_STICKY
    }

    /** Opens the GATT server and advertising unless already running. */
    private fun startBle() {
        if (ble != null) return
        val server = BleServer(this, orbit)
        val status = server.start()
        if (server.running) ble = server
        EventLog.log("ble start: $status")
        orbit.transports.update { it.copy(ble = status) }
    }

    private fun stopBle(reason: String) {
        ble?.let {
            it.stop()
            EventLog.log("ble stop: $reason")
        }
        ble = null
        orbit.closeSessions("ble", reason)
        orbit.transports.update { it.copy(ble = reason) }
    }

    override fun onDestroy() {
        EventLog.log("service destroyed")
        runCatching { unregisterReceiver(btReceiver) }
        val ending = orbit.shutdownSessions("service stopped")
        nsd?.stop()
        tcp?.stop()
        // Stopping BLE drops its links at once, so let their `bye` go out first.
        val deadline = System.nanoTime() + BYE_FLUSH_MS * 1_000_000
        ending.filter { it.transport == "ble" }.forEach {
            it.awaitClosed(maxOf(0, (deadline - System.nanoTime()) / 1_000_000))
        }
        stopBle("stopped")
        orbit.transports.value = TransportStatus()
        super.onDestroy()
    }

    private fun buildNotification(): Notification {
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE,
        )
        return Notification.Builder(this, CHANNEL_SERVICE)
            .setSmallIcon(R.drawable.ic_orbit)
            .setContentTitle("Orbit is sharing live activities")
            .setContentText("Paired computers nearby can see and control them")
            .setContentIntent(open)
            .setOngoing(true)
            .build()
    }

    companion object {
        private const val NOTIFICATION_ID = 1
        private const val BYE_FLUSH_MS = 1_000L
        const val ACTION_ENSURE_BLE = "dev.orbit.companion.ENSURE_BLE"
        const val ACTION_RENAME = "dev.orbit.companion.RENAME"
        private const val EXTRA_SOURCE = "source"
        private val BT_STATES = mapOf(
            BluetoothAdapter.STATE_OFF to "off", BluetoothAdapter.STATE_TURNING_ON to "turning on",
            BluetoothAdapter.STATE_ON to "on", BluetoothAdapter.STATE_TURNING_OFF to "turning off",
        )

        private const val KEY_STOPPED = "stopped_by_user"

        private fun prefs(context: Context) = context.getSharedPreferences("settings", Context.MODE_PRIVATE)

        fun stoppedByUser(context: Context) = prefs(context).getBoolean(KEY_STOPPED, false)

        /**
         * Starts serving unless the user pressed Stop; [byUser] (the Start button) clears that.
         * [source] names the caller in the diagnostics log.
         */
        fun start(context: Context, action: String? = null, source: String, byUser: Boolean = false) {
            if (byUser) {
                prefs(context).edit().putBoolean(KEY_STOPPED, false).apply()
            } else if (stoppedByUser(context)) {
                EventLog.log("service start from $source skipped: stopped by user")
                return
            }
            context.startForegroundService(
                Intent(context, OrbitService::class.java).setAction(action).putExtra(EXTRA_SOURCE, source),
            )
        }

        /** The user's Stop: stays stopped until [start] with byUser. */
        fun stop(context: Context) {
            prefs(context).edit().putBoolean(KEY_STOPPED, true).apply()
            EventLog.log("service stop by user")
            context.stopService(Intent(context, OrbitService::class.java))
        }
    }
}
