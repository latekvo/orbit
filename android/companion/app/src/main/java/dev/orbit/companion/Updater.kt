package dev.orbit.companion

import android.app.Notification
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.ActivityNotFoundException
import android.content.Context
import android.content.Intent
import android.net.Uri
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import java.io.ByteArrayOutputStream
import java.io.IOException
import java.io.InputStream
import java.net.HttpURLConnection
import java.net.SocketTimeoutException
import java.net.URL
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/**
 * Checks GitHub's latest release and offers it in the app and as a notification. Updating opens
 * the APK's URL in the browser, which downloads it; opening the download installs it.
 */
class Updater private constructor(private val context: Context) {
    private val prefs = context.getSharedPreferences("updates", Context.MODE_PRIVATE)
    private val nm = context.getSystemService(NotificationManager::class.java)
    // One thread, so concurrent requests queue and the later ones find the check no longer due.
    private val worker = Executors.newSingleThreadExecutor(daemonThreads("orbit-update") { EventLog.log("update thread died: $it") })

    private val _offer = MutableStateFlow(Releases.offer(BuildConfig.VERSION_NAME, stored(), dismissed()))

    /** The update the main screen offers, or null. */
    val offer: StateFlow<Release?> = _offer

    /** The last Update press: its version, and whether an app opened the download. */
    data class Attempt(val version: Version, val opened: Boolean)

    val attempt = MutableStateFlow<Attempt?>(null)

    /** Checks unless a check finished less than [intervalMs] ago. */
    fun checkIfDue(intervalMs: Long, source: String) {
        worker.execute {
            if (Releases.due(System.currentTimeMillis(), prefs.getLong(KEY_LAST_CHECK, 0), intervalMs)) check(source)
        }
    }

    private fun check(source: String) {
        val outcome = try {
            fetch()
        } catch (e: SocketTimeoutException) {
            Outcome.Failed("timed out")
        } catch (e: IOException) {
            Outcome.Failed("offline or unreachable ($e)")
        }
        prefs.edit().putLong(KEY_LAST_CHECK, System.currentTimeMillis()).apply()
        val installed = BuildConfig.VERSION_NAME
        if (outcome is Outcome.Failed) {
            EventLog.log("update check ($source, installed $installed): ${outcome.reason}")
            return
        }
        val r = (outcome as Outcome.Found).release
        prefs.edit().putString(KEY_VERSION, r?.version?.toString()).putString(KEY_APK, r?.apkUrl).putString(KEY_PAGE, r?.pageUrl).apply()
        val offer = Releases.offer(installed, r, dismissed())
        val line = when {
            r == null -> "no release"
            r.apkUrl == null -> "v${r.version} has no .apk asset"
            Releases.offer(installed, r, null) == null -> "up to date (latest v${r.version})"
            offer == null -> "v${r.version} available, hidden by Not now"
            else -> "v${r.version} available"
        }
        EventLog.log("update check ($source, installed $installed): $line")
        publish(offer)
    }

    private fun fetch(): Outcome {
        val conn = URL(BuildConfig.UPDATE_API_URL).openConnection() as HttpURLConnection
        try {
            conn.connectTimeout = TIMEOUT_MS
            conn.readTimeout = TIMEOUT_MS
            conn.setRequestProperty("Accept", "application/vnd.github+json")
            conn.setRequestProperty("User-Agent", "orbit-companion/${BuildConfig.VERSION_NAME}")
            return when (val code = conn.responseCode) {
                200 -> Releases.parse(conn.inputStream.use(::readCapped))?.let { Outcome.Found(it) }
                    ?: Outcome.Failed("latest release unusable (bad JSON or tag not vX.Y.Z)")
                404 -> Outcome.Found(null)
                403, 429 -> Outcome.Failed("rate limited (HTTP $code)")
                else -> Outcome.Failed("HTTP $code")
            }
        } finally {
            conn.disconnect()
        }
    }

    private fun readCapped(input: InputStream): String {
        val out = ByteArrayOutputStream()
        val buf = ByteArray(8192)
        while (out.size() < MAX_BODY) {
            val n = input.read(buf)
            if (n < 0) break
            out.write(buf, 0, n)
        }
        return out.toString(Charsets.UTF_8.name())
    }

    private fun publish(offer: Release?) {
        _offer.value = offer
        if (offer == null) {
            nm.cancel(NOTIFICATION_ID)
            return
        }
        val version = offer.version.toString()
        if (prefs.getString(KEY_NOTIFIED, null) == version || !nm.areNotificationsEnabled()) return
        prefs.edit().putString(KEY_NOTIFIED, version).apply()
        val tap = PendingIntent.getActivity(
            context, 0, Intent(context, MainActivity::class.java).setAction(ACTION_UPDATE).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        ensureChannels(context)
        nm.notify(
            NOTIFICATION_ID,
            Notification.Builder(context, CHANNEL_UPDATES)
                .setSmallIcon(R.drawable.ic_orbit)
                .setContentTitle("Orbit update available: v$version")
                .setContentText("Installed ${BuildConfig.VERSION_NAME}. Tap to download.")
                .setContentIntent(tap)
                .setAutoCancel(true)
                .build(),
        )
    }

    /** Opens the offered APK in the browser, or the release page if nothing handles the APK link. */
    fun update(from: Context) {
        val r = _offer.value ?: return
        val urls = listOfNotNull(r.apkUrl, r.pageUrl.takeIf { it.isNotEmpty() })
        val opened = urls.firstOrNull { url ->
            try {
                from.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
                true
            } catch (e: ActivityNotFoundException) {
                false
            }
        }
        EventLog.log(if (opened != null) "update v${r.version}: opened $opened" else "update v${r.version}: no app can open the download")
        nm.cancel(NOTIFICATION_ID)
        attempt.value = Attempt(r.version, opened != null)
    }

    /** Hides the offered version until a newer one is released. */
    fun dismiss() {
        val r = _offer.value ?: return
        prefs.edit().putString(KEY_DISMISSED, r.version.toString()).apply()
        EventLog.log("update v${r.version} dismissed")
        nm.cancel(NOTIFICATION_ID)
        _offer.value = null
    }

    private fun dismissed() = prefs.getString(KEY_DISMISSED, null)

    private fun stored(): Release? {
        val version = prefs.getString(KEY_VERSION, null)?.let(Version::parse) ?: return null
        return Release(version, prefs.getString(KEY_APK, null), prefs.getString(KEY_PAGE, null).orEmpty())
    }

    private sealed interface Outcome {
        /** A readable answer; null when the repository has no releases. */
        data class Found(val release: Release?) : Outcome
        data class Failed(val reason: String) : Outcome
    }

    companion object {
        const val ACTION_UPDATE = "dev.orbit.companion.UPDATE"
        val HOUR_MS = TimeUnit.HOURS.toMillis(1)
        val DAY_MS = TimeUnit.DAYS.toMillis(1)
        private const val NOTIFICATION_ID = 2
        private const val TIMEOUT_MS = 15_000
        private const val MAX_BODY = 1 shl 20
        private const val KEY_LAST_CHECK = "last_check"
        private const val KEY_VERSION = "latest_version"
        private const val KEY_APK = "latest_apk"
        private const val KEY_PAGE = "latest_page"
        private const val KEY_DISMISSED = "dismissed_version"
        private const val KEY_NOTIFIED = "notified_version"
        @Volatile private var instance: Updater? = null

        fun get(context: Context): Updater = instance ?: synchronized(this) {
            instance ?: Updater(context.applicationContext).also { instance = it }
        }
    }
}
