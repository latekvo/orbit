package dev.orbit.sample

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.graphics.drawable.Icon
import android.os.Handler
import android.os.Looper
import android.util.Log

/** A producer that owns one notification and can anchor [KeepAliveService]. */
interface Producer {
    val name: String
    val notifId: Int
    /** True while the producer needs the process kept alive (ongoing, not finished). */
    val active: Boolean
    fun buildNotification(): Notification
    /** Handles `<name>.<verb>`; returns "ok" or a short reason it was not applied. */
    fun handle(verb: String, value: Long?, text: String?): String
    fun tick() {}
    fun describe(): String
}

object Producers {
    const val TAG = "OrbitSample"
    const val ACTION = "dev.orbit.sample.ACTION"
    const val EXTRA_ACTION = "action"
    const val CH_MEDIA = "media"
    const val CH_DOWNLOAD = "download"
    const val CH_TIMER = "timer"
    const val CH_REPLY = "reply"

    lateinit var app: Context
        private set
    lateinit var nm: NotificationManager
        private set
    lateinit var media: MediaProducer
        private set
    lateinit var all: List<Producer>
        private set

    val recent = ArrayDeque<String>()
    private val handler = Handler(Looper.getMainLooper())
    private val ticker = object : Runnable {
        override fun run() {
            all.forEach { it.tick() }
            handler.postDelayed(this, 250)
        }
    }

    fun init(ctx: Context) {
        app = ctx.applicationContext
        nm = app.getSystemService(NotificationManager::class.java)
        nm.createNotificationChannels(
            listOf(
                NotificationChannel(CH_MEDIA, "Media playback", NotificationManager.IMPORTANCE_LOW),
                NotificationChannel(CH_DOWNLOAD, "Downloads", NotificationManager.IMPORTANCE_LOW),
                NotificationChannel(CH_TIMER, "Timers", NotificationManager.IMPORTANCE_LOW),
                NotificationChannel(CH_REPLY, "Messages", NotificationManager.IMPORTANCE_LOW),
            )
        )
        media = MediaProducer(app)
        all = listOf(media, DownloadProducer(app), TimerProducer(app), ReplyProducer(app))
        handler.post(ticker)
    }

    /**
     * Each producer gets its own group so the system does not auto-bundle them under one
     * "ranker_group" summary, which real apps (one producer per package) would never get.
     */
    fun group(producer: String) = "dev.orbit.sample.$producer"

    fun byName(name: String): Producer? = all.firstOrNull { it.name == name }

    /** Parses "media,download" or "all" into producer names. */
    fun parseList(csv: String?): List<String> {
        if (csv.isNullOrBlank()) return emptyList()
        val names = csv.split(',').map { it.trim().lowercase() }.filter { it.isNotEmpty() }
        return if ("all" in names) all.map { it.name } else names
    }

    /** Single entry point for every action; logs `action=<name> ...` with the resulting state. */
    fun dispatch(action: String, value: Long? = null, text: String? = null, source: String) {
        val producerName = action.substringBefore('.')
        val verb = action.substringAfter('.', "")
        val producer = byName(producerName)
        val result = when {
            producer == null || verb.isEmpty() -> "error(unknown action)"
            else -> try {
                producer.handle(verb, value, text)
            } catch (e: Exception) {
                "error(${e.javaClass.simpleName}: ${e.message})"
            }
        }
        val line = buildString {
            append("action=").append(action)
            if (value != null) append(" value=").append(value)
            if (text != null) append(" text=\"").append(text).append('"')
            append(" source=").append(source)
            append(" result=").append(result)
            append(" state=").append(producer?.describe() ?: "-")
        }
        record(line)
    }

    /** Logs a state change the producer made on its own (not caused by an action). */
    fun event(name: String, producer: Producer) = record("event=$name state=${producer.describe()}")

    private fun record(line: String) {
        Log.i(TAG, line)
        recent.addFirst(line)
        while (recent.size > 12) recent.removeLast()
    }

    /** Re-picks the keep-alive anchor; call after a non-media producer's [Producer.active] changes. */
    fun activityChanged() {
        if (all.any { it !== media && it.active }) KeepAliveService.ensure(app) else KeepAliveService.instance?.reanchor()
    }

    fun actionIntent(ctx: Context, action: String, mutable: Boolean = false): PendingIntent {
        val intent = Intent(ctx, ActionReceiver::class.java).setAction(ACTION).putExtra(EXTRA_ACTION, action)
        val flags = PendingIntent.FLAG_UPDATE_CURRENT or
            if (mutable) PendingIntent.FLAG_MUTABLE else PendingIntent.FLAG_IMMUTABLE
        return PendingIntent.getBroadcast(ctx, action.hashCode(), intent, flags)
    }

    fun action(ctx: Context, icon: Int, label: String, action: String): Notification.Action =
        Notification.Action.Builder(Icon.createWithResource(ctx, icon), label, actionIntent(ctx, action)).build()

    fun openApp(ctx: Context): PendingIntent = PendingIntent.getActivity(
        ctx, 0,
        Intent(ctx, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),
        PendingIntent.FLAG_IMMUTABLE,
    )

    /** m:ss; [ceil] rounds up so a countdown shows 0:00 only when it has finished. */
    fun fmt(ms: Long, ceil: Boolean = false): String {
        val s = (ms.coerceAtLeast(0) + if (ceil) 999 else 0) / 1000
        return "%d:%02d".format(s / 60, s % 60)
    }
}
