package dev.orbit.companion

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.app.Notification
import android.provider.Settings
import dev.orbit.protocol.ResponderSession

const val CHANNEL_PAIRING = "pairing"
const val CHANNEL_SERVICE = "service"
const val CHANNEL_UPDATES = "updates"

fun ensureChannels(context: Context) {
    val nm = context.getSystemService(NotificationManager::class.java)
    nm.createNotificationChannel(
        NotificationChannel(CHANNEL_SERVICE, "Orbit service", NotificationManager.IMPORTANCE_MIN).apply {
            setShowBadge(false)
        },
    )
    nm.createNotificationChannel(NotificationChannel(CHANNEL_PAIRING, "Pairing requests", NotificationManager.IMPORTANCE_HIGH))
    nm.createNotificationChannel(NotificationChannel(CHANNEL_UPDATES, "Updates", NotificationManager.IMPORTANCE_LOW))
}

/** High-priority notification asking the user to confirm a pairing code. */
class PairingNotifier(private val context: Context) {
    private val nm = context.getSystemService(NotificationManager::class.java)

    fun show(session: ResponderSession) {
        ensureChannels(context)
        val sas = session.sas ?: return
        val name = session.peer?.name ?: "a computer"
        val open = PendingIntent.getActivity(
            context, session.id.toInt(),
            Intent(context, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val n = Notification.Builder(context, CHANNEL_PAIRING)
            .setSmallIcon(R.drawable.ic_orbit)
            .setContentTitle("Pair with $name?")
            .setContentText("Confirm the code matches: ${sas.substring(0, 3)} ${sas.substring(3)}")
            .setCategory(Notification.CATEGORY_RECOMMENDATION)
            .setContentIntent(open)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            // Swiping the prompt away (possible for ongoing notifications on API 34+) rejects.
            .setDeleteIntent(decision(session.id, false))
            .addAction(Notification.Action.Builder(null, "Reject", decision(session.id, false)).build())
            .addAction(Notification.Action.Builder(null, "Accept", decision(session.id, true)).build())
            .build()
        if (canNotify()) {
            nm.notify(notificationId(session.id), n)
            return
        }
        // Without notifications, Orbit's own screen shows the prompt; it may only be opened from
        // the background with "Display over other apps".
        if (!Orbit.get(context).uiVisible) {
            if (Settings.canDrawOverlays(context)) {
                context.startActivity(Intent(context, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP))
            } else {
                EventLog.log("pairing prompt for $name not shown: notifications are off and Orbit is in the background")
            }
        }
    }

    private fun canNotify(): Boolean =
        nm.areNotificationsEnabled() && (nm.getNotificationChannel(CHANNEL_PAIRING)?.importance ?: 0) != NotificationManager.IMPORTANCE_NONE

    fun cancel(sessionId: Long) = nm.cancel(notificationId(sessionId))

    private fun decision(sessionId: Long, accept: Boolean): PendingIntent = PendingIntent.getBroadcast(
        context, (sessionId * 2 + if (accept) 1 else 0).toInt(),
        Intent(context, PairingReceiver::class.java)
            .putExtra(PairingReceiver.EXTRA_SESSION, sessionId)
            .putExtra(PairingReceiver.EXTRA_ACCEPT, accept),
        PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
    )

    private fun notificationId(sessionId: Long) = 1000 + sessionId.toInt()
}

class PairingReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val id = intent.getLongExtra(EXTRA_SESSION, -1)
        Orbit.get(context).session(id)?.decidePairing(intent.getBooleanExtra(EXTRA_ACCEPT, false))
    }

    companion object {
        const val EXTRA_SESSION = "session"
        const val EXTRA_ACCEPT = "accept"
    }
}
