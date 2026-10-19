package dev.orbit.sample

import android.app.Notification
import android.app.RemoteInput
import android.content.Context
import android.graphics.drawable.Icon

/** Ongoing chat notification with a RemoteInput "Reply" action; replies update the notification text. */
class ReplyProducer(private val ctx: Context) : Producer {
    override val name = "reply"
    override val notifId = 1004
    override var active = false
        private set

    val replies = ArrayList<String>()

    override fun handle(verb: String, value: Long?, text: String?): String {
        when {
            verb == "start" -> {
                active = true
                replies.clear()
                Producers.nm.notify(notifId, buildNotification())
                Producers.activityChanged()
            }
            verb == "stop" -> {
                active = false
                Producers.activityChanged()
                Producers.nm.cancel(notifId)
            }
            !active -> return "ignored(not active)"
            verb == "send" -> {
                if (text.isNullOrBlank()) return "error(missing text)"
                replies.add(text)
                // Re-posting is what clears the shade's "sending" spinner after a RemoteInput reply.
                Producers.nm.notify(notifId, buildNotification())
            }
            else -> return "error(unknown verb)"
        }
        return "ok"
    }

    override fun buildNotification(): Notification {
        val input = RemoteInput.Builder(KEY_REPLY).setLabel("Message Ada").build()
        val reply = Notification.Action.Builder(
            Icon.createWithResource(ctx, android.R.drawable.ic_menu_send),
            "Reply",
            Producers.actionIntent(ctx, "reply.send", mutable = true),
        )
            .addRemoteInput(input)
            .setAllowGeneratedReplies(false)
            .setSemanticAction(Notification.Action.SEMANTIC_ACTION_REPLY)
            .build()
        val last = replies.lastOrNull()
        return Notification.Builder(ctx, Producers.CH_REPLY)
            .setSmallIcon(android.R.drawable.sym_action_chat)
            .setContentTitle("Ada (Orbit QA)")
            .setContentText(if (last == null) PROMPT else "You: $last")
            .setSubText(if (replies.isEmpty()) null else "${replies.size} sent")
            .setContentIntent(Producers.openApp(ctx))
            .setCategory(Notification.CATEGORY_MESSAGE)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setColor(0xFF3C7BE8.toInt())
            .setRemoteInputHistory(replies.reversed().toTypedArray<CharSequence>())
            .addAction(reply)
            .build()
    }

    override fun describe() =
        "reply{active=$active count=${replies.size}" + (replies.lastOrNull()?.let { " last=\"$it\"" } ?: "") + "}"

    companion object {
        const val KEY_REPLY = "orbit_reply"
        const val PROMPT = "Are you seeing this on your laptop? Reply here."
    }
}
