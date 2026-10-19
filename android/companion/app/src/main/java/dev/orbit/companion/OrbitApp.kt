package dev.orbit.companion

import android.app.Application
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

class OrbitApp : Application() {
    override fun onCreate() {
        super.onCreate()
        ensureChannels(this)
    }
}

class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action == Intent.ACTION_BOOT_COMPLETED) OrbitService.start(context)
    }
}
