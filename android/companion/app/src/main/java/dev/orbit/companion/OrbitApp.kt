package dev.orbit.companion

import android.app.Application
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

class OrbitApp : Application() {
    override fun onCreate() {
        super.onCreate()
        EventLog.init(this)
        EventLog.logProcessStart(this)
        ensureChannels(this)
    }
}

/** Starts serving after a reboot or an app update (both may start a foreground service from the background). */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val source = when (intent.action) {
            Intent.ACTION_BOOT_COMPLETED -> "boot"
            Intent.ACTION_MY_PACKAGE_REPLACED -> "app updated"
            else -> return
        }
        try {
            OrbitService.start(context, source = source)
        } catch (e: IllegalStateException) {
            EventLog.log("service start on $source refused: $e")
        }
    }
}
