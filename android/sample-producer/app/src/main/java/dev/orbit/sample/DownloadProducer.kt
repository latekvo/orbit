package dev.orbit.sample

import android.app.Notification
import android.content.Context
import android.os.SystemClock

/** Fake 150 MB download advancing 0 -> 100% over 60 s of unpaused time. */
class DownloadProducer(private val ctx: Context) : Producer {
    override val name = "download"
    override val notifId = 1002
    override var active = false
        private set

    private var paused = false
    private var finished = false
    private var progress = 0.0
    private var lastTick = 0L

    private val percent get() = progress.toInt().coerceIn(0, 100)

    override fun handle(verb: String, value: Long?, text: String?): String {
        if (verb == "start") {
            progress = 0.0
            paused = false
            finished = false
            lastTick = SystemClock.elapsedRealtime()
            active = true
            Producers.nm.notify(notifId, buildNotification())
            Producers.activityChanged()
            return "ok"
        }
        if (verb == "stop" || (verb == "cancel" && active)) {
            active = false
            finished = false
            Producers.activityChanged()
            Producers.nm.cancel(notifId)
            return "ok"
        }
        if (!active) return "ignored(not active)"
        when (verb) {
            "pause" -> if (paused) return "ignored(already paused)" else paused = true
            "resume" -> if (!paused) return "ignored(not paused)" else {
                paused = false
                lastTick = SystemClock.elapsedRealtime()
            }
            else -> return "error(unknown verb)"
        }
        Producers.nm.notify(notifId, buildNotification())
        return "ok"
    }

    override fun tick() {
        if (!active || paused) return
        val now = SystemClock.elapsedRealtime()
        val before = percent
        progress += (now - lastTick) * 100.0 / DURATION_MS
        lastTick = now
        if (progress >= 100) {
            progress = 100.0
            finished = true
            active = false
            Producers.activityChanged()
            Producers.nm.notify(notifId, buildNotification())
            Producers.event("download.complete", this)
        } else if (percent != before) {
            Producers.nm.notify(notifId, buildNotification())
        }
    }

    override fun buildNotification(): Notification {
        val b = Notification.Builder(ctx, Producers.CH_DOWNLOAD)
            .setGroup(Producers.group(name))
            .setContentIntent(Producers.openApp(ctx))
            .setCategory(Notification.CATEGORY_PROGRESS)
            .setOnlyAlertOnce(true)
            .setShowWhen(false)
            .setSubText("Orbit Downloads")
            .setColor(0xFF6750A4.toInt())
        if (finished) {
            return b.setSmallIcon(android.R.drawable.stat_sys_download_done)
                .setContentTitle("Download complete")
                .setContentText("$FILE - 150 MB")
                .setOngoing(false)
                .setAutoCancel(true)
                .build()
        }
        val mb = 150.0 * progress / 100
        return b.setSmallIcon(android.R.drawable.stat_sys_download)
            .setContentTitle("Downloading $FILE")
            .setContentText(
                if (paused) "Paused - $percent%" else "$percent% - " + "%.1f MB of 150 MB".format(mb)
            )
            .setProgress(100, percent, false)
            .setOngoing(true)
            .addAction(
                if (paused) Producers.action(ctx, android.R.drawable.ic_media_play, "Resume", "download.resume")
                else Producers.action(ctx, android.R.drawable.ic_media_pause, "Pause", "download.pause")
            )
            .addAction(Producers.action(ctx, android.R.drawable.ic_menu_close_clear_cancel, "Cancel", "download.cancel"))
            .build()
    }

    override fun describe() = "download{active=$active paused=$paused finished=$finished progress=$percent%}"

    companion object {
        const val FILE = "orbit-dataset.zip"
        const val DURATION_MS = 60_000.0
    }
}
