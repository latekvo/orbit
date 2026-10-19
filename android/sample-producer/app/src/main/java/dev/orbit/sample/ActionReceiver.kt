package dev.orbit.sample

import android.app.RemoteInput
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/**
 * Receives every notification action (explicit PendingIntents) and automation broadcasts:
 * extras `action` (e.g. `download.pause`), optional `value` (long/int/string) and `text`,
 * or `start` / `stop` with a producer list.
 */
class ActionReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        Producers.parseList(intent.getStringExtra("stop")).forEach { Producers.dispatch("$it.stop", source = "broadcast") }
        Producers.parseList(intent.getStringExtra("start")).forEach { Producers.dispatch("$it.start", source = "broadcast") }
        val action = intent.getStringExtra(Producers.EXTRA_ACTION) ?: return
        val remote = RemoteInput.getResultsFromIntent(intent)?.getCharSequence(ReplyProducer.KEY_REPLY)?.toString()
        @Suppress("DEPRECATION")
        val value = when (val v = intent.extras?.get("value")) {
            is Number -> v.toLong()
            is String -> v.toLongOrNull()
            else -> null
        }
        Producers.dispatch(
            action,
            value,
            remote ?: intent.getStringExtra("text"),
            source = if (remote != null) "remote_input" else "broadcast",
        )
    }
}
