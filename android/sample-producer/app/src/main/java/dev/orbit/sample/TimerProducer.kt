package dev.orbit.sample

import android.app.Notification
import android.content.Context
import android.os.Build

/** 5-minute countdown shown with a count-down chronometer whose `when` is the wall-clock end time. */
class TimerProducer(private val ctx: Context) : Producer {
    override val name = "timer"
    override val notifId = 1003
    override var active = false
        private set

    private var running = false
    private var finished = false
    /** Wall-clock end time while running. */
    private var endAt = 0L
    /** Remaining time while paused. */
    private var remainingPaused = 0L

    private fun remaining(): Long =
        if (running) (endAt - System.currentTimeMillis()).coerceAtLeast(0) else remainingPaused

    override fun handle(verb: String, value: Long?, text: String?): String {
        when {
            verb == "start" -> {
                active = true
                finished = false
                running = true
                endAt = System.currentTimeMillis() + DEFAULT_MS
                Producers.nm.notify(notifId, buildNotification())
                Producers.activityChanged()
                return "ok"
            }
            verb == "stop" -> {
                active = false
                finished = false
                running = false
                Producers.activityChanged()
                Producers.nm.cancel(notifId)
                return "ok"
            }
            !active -> return "ignored(not active)"
            verb == "pause" -> {
                if (!running) return "ignored(not running)"
                remainingPaused = remaining()
                running = false
            }
            verb == "resume" -> {
                if (running) return "ignored(already running)"
                endAt = System.currentTimeMillis() + remainingPaused
                running = true
            }
            verb == "add_minute" -> if (running) endAt += 60_000 else remainingPaused += 60_000
            else -> return "error(unknown verb)"
        }
        Producers.nm.notify(notifId, buildNotification())
        return "ok"
    }

    override fun tick() {
        if (active && running && remaining() == 0L) {
            running = false
            remainingPaused = 0
            finished = true
            active = false
            Producers.activityChanged()
            Producers.nm.notify(notifId, buildNotification())
            Producers.event("timer.finished", this)
        }
    }

    override fun buildNotification(): Notification {
        val b = Notification.Builder(ctx, Producers.CH_TIMER)
            .setSmallIcon(android.R.drawable.ic_lock_idle_alarm)
            .setContentTitle("Tea timer")
            .setContentIntent(Producers.openApp(ctx))
            .setCategory(if (Build.VERSION.SDK_INT >= 31) Notification.CATEGORY_STOPWATCH else Notification.CATEGORY_ALARM)
            .setOnlyAlertOnce(true)
            .setColor(0xFFE8A33C.toInt())
        if (finished) {
            return b.setContentText("Time's up").setOngoing(false).setAutoCancel(true).setShowWhen(false).build()
        }
        b.setOngoing(true)
        if (running) {
            b.setUsesChronometer(true)
                .setChronometerCountDown(true)
                .setWhen(endAt)
                .setShowWhen(true)
                .setContentText("Running - ends at %tR".format(endAt))
        } else {
            b.setUsesChronometer(false)
                .setShowWhen(false)
                .setContentText("Paused - ${Producers.fmt(remainingPaused, ceil = true)} left")
        }
        return b
            .addAction(
                if (running) Producers.action(ctx, android.R.drawable.ic_media_pause, "Pause", "timer.pause")
                else Producers.action(ctx, android.R.drawable.ic_media_play, "Resume", "timer.resume")
            )
            .addAction(Producers.action(ctx, android.R.drawable.ic_input_add, "+1 min", "timer.add_minute"))
            .build()
    }

    override fun describe() =
        "timer{active=$active running=$running finished=$finished remaining=${remaining()} (${Producers.fmt(remaining(), ceil = true)})" +
            (if (running) " endAt=$endAt" else "") + "}"

    companion object {
        const val DEFAULT_MS = 5 * 60_000L
    }
}
