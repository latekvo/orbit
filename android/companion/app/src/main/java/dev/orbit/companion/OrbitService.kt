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
import android.util.Log
import kotlinx.coroutines.flow.update

/** Foreground service hosting the TCP server, DNS-SD registration and the BLE peripheral. */
class OrbitService : Service() {
    private lateinit var orbit: Orbit
    private var tcp: TcpServer? = null
    private var nsd: NsdAdvertiser? = null
    private var ble: BleServer? = null

    private val btReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            when (intent.getIntExtra(BluetoothAdapter.EXTRA_STATE, -1)) {
                BluetoothAdapter.STATE_ON -> startBle()
                BluetoothAdapter.STATE_TURNING_OFF, BluetoothAdapter.STATE_OFF -> stopBle("bluetooth off")
            }
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        orbit = Orbit.get(this)
        startForeground(NOTIFICATION_ID, buildNotification(), ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE)
        val server = TcpServer(orbit)
        val port = server.start()
        tcp = server
        orbit.transports.update { it.copy(serviceRunning = true, tcpPort = port) }
        nsd = NsdAdvertiser(this) { s -> orbit.transports.update { it.copy(nsd = s) } }.also {
            it.start(orbit.deviceName, port, orbit.deviceId)
        }
        registerReceiver(btReceiver, IntentFilter(BluetoothAdapter.ACTION_STATE_CHANGED))
        startBle()
        Log.i(TAG, "service started: tcp port $port, deviceId ${orbit.deviceId}")
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_RESTART_BLE) {
            stopBle("restarting")
            startBle()
        }
        return START_STICKY
    }

    private fun startBle() {
        if (ble != null) return
        val server = BleServer(this, orbit)
        val status = server.start()
        if (server.running) ble = server
        orbit.transports.update { it.copy(ble = status) }
    }

    private fun stopBle(reason: String) {
        ble?.stop()
        ble = null
        orbit.closeSessions("ble", reason)
        orbit.transports.update { it.copy(ble = reason) }
    }

    override fun onDestroy() {
        runCatching { unregisterReceiver(btReceiver) }
        orbit.shutdownSessions("service stopped")
        nsd?.stop()
        tcp?.stop()
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
        const val ACTION_RESTART_BLE = "dev.orbit.companion.RESTART_BLE"

        fun start(context: Context, action: String? = null) {
            context.startForegroundService(Intent(context, OrbitService::class.java).setAction(action))
        }

        fun stop(context: Context) {
            context.stopService(Intent(context, OrbitService::class.java))
        }
    }
}
