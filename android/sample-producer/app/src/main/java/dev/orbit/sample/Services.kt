package dev.orbit.sample

import android.app.Notification
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.IBinder

/** Foreground service (mediaPlayback) whose notification is the media player's. */
class MediaService : Service() {
    override fun onCreate() {
        instance = this
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val media = Producers.media
        startForeground(media.notifId, media.buildNotification(), ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK)
        // The player may have been stopped between startForegroundService() and now.
        if (!media.active) dismiss()
        return START_NOT_STICKY
    }

    fun dismiss() {
        stopForeground(STOP_FOREGROUND_REMOVE)
        instance = null
        stopSelf()
    }

    override fun onDestroy() {
        if (instance === this) instance = null
    }

    override fun onBind(intent: Intent?): IBinder? = null

    companion object {
        var instance: MediaService? = null
            private set
    }
}

/**
 * Foreground service (dataSync) that keeps the process from being frozen while a download, timer
 * or reply producer is active. It borrows one active producer's notification instead of posting its
 * own, so mirrors see no extra "app is running" activity.
 */
class KeepAliveService : Service() {
    private var anchorId = 0

    override fun onCreate() {
        instance = this
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (Producers.all.none { it !== Producers.media && it.active }) {
            // Every producer stopped before this start was delivered; startForeground is still mandatory.
            startForeground(
                PLACEHOLDER_ID,
                Notification.Builder(this, Producers.CH_DOWNLOAD).setSmallIcon(android.R.drawable.stat_sys_download).build(),
                ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC,
            )
            stopForeground(STOP_FOREGROUND_REMOVE)
        }
        reanchor()
        return START_NOT_STICKY
    }

    fun reanchor() {
        val candidates = Producers.all.filter { it !== Producers.media && it.active }
        if (candidates.isEmpty()) {
            stopForeground(STOP_FOREGROUND_DETACH)
            anchorId = 0
            instance = null
            stopSelf()
            return
        }
        if (candidates.any { it.notifId == anchorId }) return
        val p = candidates.first()
        // Switching ids makes the system cancel the previous anchor's notification; callers re-post
        // or cancel it themselves after this returns.
        startForeground(p.notifId, p.buildNotification(), ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
        anchorId = p.notifId
    }

    override fun onDestroy() {
        if (instance === this) instance = null
    }

    override fun onBind(intent: Intent?): IBinder? = null

    companion object {
        var instance: KeepAliveService? = null
            private set
        private const val PLACEHOLDER_ID = 1999

        fun ensure(ctx: Context) {
            instance?.reanchor() ?: ctx.startForegroundService(Intent(ctx, KeepAliveService::class.java))
        }
    }
}
