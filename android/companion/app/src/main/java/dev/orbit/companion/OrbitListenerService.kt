package dev.orbit.companion

import android.app.ActivityOptions
import android.app.Notification
import android.app.PendingIntent
import android.app.RemoteInput
import android.content.ComponentName
import android.content.Intent
import android.media.MediaMetadata
import android.media.session.MediaController
import android.media.session.MediaSession
import android.media.session.MediaSessionManager
import android.media.session.PlaybackState
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.HandlerThread
import android.os.SystemClock
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import android.util.Log
import dev.orbit.protocol.Art
import dev.orbit.protocol.POSITION_AT
import dev.orbit.protocol.sha256
import dev.orbit.protocol.str
import dev.orbit.protocol.toHex
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonObjectBuilder
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonObject

/**
 * Sources live activities (spec 5.1) from posted notifications and active media sessions, and
 * executes `action` messages against them.
 */
class OrbitListenerService : NotificationListenerService(), ActionExecutor {
    private lateinit var orbit: Orbit
    private lateinit var arts: ArtCache
    private lateinit var thread: HandlerThread
    private lateinit var handler: Handler
    private var sessionManager: MediaSessionManager? = null

    // Worker-thread state.
    private val notifs = HashMap<String, StatusBarNotification>() // activity id -> source
    private val allNotifs = HashMap<String, StatusBarNotification>() // sbn key -> latest
    private val controllers = LinkedHashMap<String, MediaController>() // activity id -> controller
    private val callbacks = HashMap<MediaSession.Token, MediaController.Callback>()

    private val sessionsListener = MediaSessionManager.OnActiveSessionsChangedListener { list ->
        handler.post { updateControllers(list.orEmpty()) }
    }

    override fun onCreate() {
        super.onCreate()
        orbit = Orbit.get(this)
        arts = ArtCache(this)
        thread = HandlerThread("orbit-source").apply { start() }
        handler = Handler(thread.looper)
    }

    override fun onDestroy() {
        thread.quitSafely()
        super.onDestroy()
    }

    override fun onListenerConnected() {
        orbit.actionExecutor = this
        orbit.listenerConnected.value = true
        // The system keeps this listener bound, so it is the most reliable place to (re)start serving.
        try {
            OrbitService.start(this)
        } catch (e: IllegalStateException) {
            Log.w(TAG, "could not start the foreground service from the listener", e)
        }
        val active = runCatching { activeNotifications?.toList().orEmpty() }.getOrDefault(emptyList())
        handler.post {
            val component = ComponentName(this, OrbitListenerService::class.java)
            try {
                val sm = getSystemService(MediaSessionManager::class.java)
                sessionManager = sm
                sm.addOnActiveSessionsChangedListener(sessionsListener, component, handler)
                updateControllers(sm.getActiveSessions(component))
            } catch (e: SecurityException) {
                Log.w(TAG, "media sessions unavailable", e)
            }
            active.forEach { onPosted(it) }
        }
    }

    override fun onListenerDisconnected() {
        if (orbit.actionExecutor === this) orbit.actionExecutor = null
        orbit.listenerConnected.value = false
        handler.post {
            runCatching { sessionManager?.removeOnActiveSessionsChangedListener(sessionsListener) }
            updateControllers(emptyList())
            (notifs.keys.toList()).forEach { orbit.publisher.remove(it) }
            notifs.clear()
            allNotifs.clear()
        }
    }

    override fun onNotificationPosted(sbn: StatusBarNotification) {
        handler.post { onPosted(sbn) }
    }

    override fun onNotificationRemoved(sbn: StatusBarNotification) {
        handler.post {
            allNotifs.remove(sbn.key)
            val id = notifId(sbn)
            if (notifs.remove(id) != null) orbit.publisher.remove(id)
        }
    }

    // --- notifications ---

    private fun notifId(sbn: StatusBarNotification) = "notif:${sbn.packageName}:${sha256(sbn.key.toByteArray()).toHex().take(10)}"

    private fun onPosted(sbn: StatusBarNotification) {
        allNotifs[sbn.key] = sbn
        val id = notifId(sbn)
        val activity = if (isLive(sbn) && !isExposedMedia(sbn)) map(sbn, id) else null
        if (activity == null) {
            if (notifs.remove(id) != null) orbit.publisher.remove(id)
            return
        }
        notifs[id] = sbn
        orbit.publisher.submit(activity.first, activity.second)
    }

    private fun isExposedMedia(sbn: StatusBarNotification): Boolean {
        val token = sbn.notification.extras.parcelable<MediaSession.Token>(Notification.EXTRA_MEDIA_SESSION) ?: return false
        return controllers.values.any { it.sessionToken == token }
    }

    private fun isLive(sbn: StatusBarNotification): Boolean {
        val pkg = sbn.packageName
        if (pkg == packageName || pkg in EXCLUDED_PACKAGES) return false
        val n = sbn.notification
        if (n.flags and Notification.FLAG_GROUP_SUMMARY != 0) return false
        val extras = n.extras
        val hasActions = !n.actions.isNullOrEmpty()
        return n.category in LIVE_CATEGORIES ||
            hasProgress(extras) ||
            extras.getBoolean(Notification.EXTRA_SHOW_CHRONOMETER) ||
            (Build.VERSION.SDK_INT >= 36 && n.flags and FLAG_PROMOTED_ONGOING != 0) ||
            ((n.flags and (Notification.FLAG_ONGOING_EVENT or Notification.FLAG_FOREGROUND_SERVICE)) != 0 && hasActions)
    }

    private fun hasProgress(extras: Bundle) =
        extras.getInt(Notification.EXTRA_PROGRESS_MAX) > 0 || extras.getBoolean(Notification.EXTRA_PROGRESS_INDETERMINATE)

    private fun map(sbn: StatusBarNotification, id: String): Pair<JsonObject, List<Art>> {
        val n = sbn.notification
        val e = n.extras
        val newArts = mutableListOf<Art>()
        val icon = arts.appIcon(sbn.packageName)?.also { newArts += it }
        val large = (n.getLargeIcon() ?: e.parcelable<android.graphics.drawable.Icon>(Notification.EXTRA_LARGE_ICON))
            ?.let { arts.art(it) }?.also { newArts += it }
        val chrono = e.getBoolean(Notification.EXTRA_SHOW_CHRONOMETER)
        val kind = when {
            n.category == Notification.CATEGORY_TRANSPORT -> "media"
            n.category == Notification.CATEGORY_CALL -> "call"
            n.category == Notification.CATEGORY_NAVIGATION -> "navigation"
            n.category == Notification.CATEGORY_PROGRESS || hasProgress(e) -> "progress"
            n.category == CATEGORY_STOPWATCH || n.category == Notification.CATEGORY_ALARM || chrono -> "timer"
            else -> "generic"
        }
        val title = (e.getCharSequence(Notification.EXTRA_TITLE) ?: e.getCharSequence(Notification.EXTRA_TITLE_BIG))?.toString()
        val activity = buildJsonObject {
            put("id", id)
            put("kind", kind)
            putJsonObject("app") { put("id", sbn.packageName); put("name", appName(sbn.packageName)) }
            put("title", title?.takeIf { it.isNotBlank() } ?: appName(sbn.packageName))
            e.getCharSequence(Notification.EXTRA_SUB_TEXT)?.toString()?.takeIf { it.isNotBlank() }?.let { put("subtitle", it) }
            (e.getCharSequence(Notification.EXTRA_TEXT) ?: e.getCharSequence(Notification.EXTRA_BIG_TEXT))
                ?.toString()?.takeIf { it.isNotBlank() }?.let { put("text", it) }
            icon?.let { put("iconKey", it.key) }
            large?.let { put("artKey", it.key) }
            if (n.color != Notification.COLOR_DEFAULT) put("color", "#%06x".format(n.color and 0xffffff))
            if (hasProgress(e)) {
                putJsonObject("progress") {
                    put("value", e.getInt(Notification.EXTRA_PROGRESS))
                    put("max", e.getInt(Notification.EXTRA_PROGRESS_MAX))
                    put("indeterminate", e.getBoolean(Notification.EXTRA_PROGRESS_INDETERMINATE))
                }
            }
            if (chrono && n.`when` > 0) {
                putJsonObject("chrono") {
                    put("baseMs", n.`when`)
                    put("countDown", e.getBoolean(Notification.EXTRA_CHRONOMETER_COUNT_DOWN))
                    put("running", true)
                }
            }
            put("actions", buildJsonArray {
                n.actions.orEmpty().forEachIndexed { i, a ->
                    add(action("notif.$i", a.title?.toString().orEmpty().ifBlank { "Action ${i + 1}" }) {
                        a.remoteInputs?.firstOrNull { it.allowFreeFormInput }?.let { ri ->
                            putJsonObject("input") { put("kind", "text"); put("label", ri.label?.toString() ?: "Reply") }
                        }
                    })
                }
                if (n.contentIntent != null) add(action("open", "Open"))
                if (sbn.isClearable) add(action("dismiss", "Dismiss", icon = "close"))
            })
        }
        return activity to newArts
    }

    // --- media sessions ---

    private fun mediaId(c: MediaController) = "media:${c.packageName}"

    private fun updateControllers(list: List<MediaController>) {
        val wanted = LinkedHashMap<String, MediaController>()
        list.forEach { c -> if (c.packageName != packageName) wanted.putIfAbsent(mediaId(c), c) }
        for ((id, c) in controllers.toMap()) {
            if (wanted[id]?.sessionToken != c.sessionToken) {
                callbacks.remove(c.sessionToken)?.let { runCatching { c.unregisterCallback(it) } }
                controllers.remove(id)
                orbit.publisher.remove(id)
            }
        }
        for ((id, c) in wanted) {
            if (controllers.containsKey(id)) continue
            controllers[id] = c
            val cb = object : MediaController.Callback() {
                override fun onPlaybackStateChanged(state: PlaybackState?) = publishMedia(id)
                override fun onMetadataChanged(metadata: MediaMetadata?) = publishMedia(id)
                override fun onSessionDestroyed() {
                    if (controllers[id]?.sessionToken == c.sessionToken) {
                        controllers.remove(id)
                        callbacks.remove(c.sessionToken)
                        orbit.publisher.remove(id)
                        refreshNotifications()
                    }
                }
            }
            callbacks[c.sessionToken] = cb
            c.registerCallback(cb, handler)
            publishMedia(id)
        }
        refreshNotifications()
    }

    /** Re-evaluates notifications whose media-session suppression may have changed. */
    private fun refreshNotifications() {
        allNotifs.values.toList().forEach { onPosted(it) }
    }

    private fun publishMedia(id: String) {
        val c = controllers[id] ?: return
        val state = c.playbackState
        val meta = c.metadata
        val title = meta?.getString(MediaMetadata.METADATA_KEY_TITLE) ?: meta?.getString(MediaMetadata.METADATA_KEY_DISPLAY_TITLE)
        val st = when (state?.state) {
            PlaybackState.STATE_PLAYING, PlaybackState.STATE_FAST_FORWARDING, PlaybackState.STATE_REWINDING -> "playing"
            PlaybackState.STATE_PAUSED -> "paused"
            PlaybackState.STATE_BUFFERING, PlaybackState.STATE_CONNECTING,
            PlaybackState.STATE_SKIPPING_TO_NEXT, PlaybackState.STATE_SKIPPING_TO_PREVIOUS,
            PlaybackState.STATE_SKIPPING_TO_QUEUE_ITEM -> "buffering"
            PlaybackState.STATE_STOPPED -> if (title != null) "stopped" else null
            else -> null
        }
        if (st == null || state == null) {
            orbit.publisher.remove(id)
            return
        }
        val newArts = mutableListOf<Art>()
        val icon = arts.appIcon(c.packageName)?.also { newArts += it }
        val bmp = meta?.getBitmap(MediaMetadata.METADATA_KEY_ART) ?: meta?.getBitmap(MediaMetadata.METADATA_KEY_ALBUM_ART)
            ?: meta?.getBitmap(MediaMetadata.METADATA_KEY_DISPLAY_ICON)
        val art = arts.art(bmp)?.also { newArts += it }
        val actions = state.actions
        fun has(a: Long) = actions and a != 0L
        val duration = meta?.getLong(MediaMetadata.METADATA_KEY_DURATION)?.takeIf { it > 0 }
        val positionAt = System.currentTimeMillis() - (SystemClock.elapsedRealtime() - state.lastPositionUpdateTime)
        val activity = buildJsonObject {
            put("id", id)
            put("kind", "media")
            putJsonObject("app") { put("id", c.packageName); put("name", appName(c.packageName)) }
            put("title", title ?: appName(c.packageName))
            (meta?.getString(MediaMetadata.METADATA_KEY_ARTIST) ?: meta?.getString(MediaMetadata.METADATA_KEY_DISPLAY_SUBTITLE))
                ?.let { put("subtitle", it) }
            meta?.getString(MediaMetadata.METADATA_KEY_ALBUM)?.let { put("text", it) }
            icon?.let { put("iconKey", it.key) }
            art?.let { put("artKey", it.key) }
            putJsonObject("media") {
                put("state", st)
                if (state.position >= 0) {
                    put("positionMs", state.position)
                    put(POSITION_AT, positionAt)
                }
                duration?.let { put("durationMs", it) }
                put("rate", if (st == "playing") state.playbackSpeed.toDouble() else 1.0)
                put("seekable", has(PlaybackState.ACTION_SEEK_TO))
            }
            put("actions", buildJsonArray {
                if (has(PlaybackState.ACTION_SKIP_TO_PREVIOUS)) add(action("media.previous", "Previous", "previous"))
                if (st == "playing") {
                    if (has(PlaybackState.ACTION_PAUSE) || has(PlaybackState.ACTION_PLAY_PAUSE)) add(action("media.pause", "Pause", "pause"))
                } else if (has(PlaybackState.ACTION_PLAY) || has(PlaybackState.ACTION_PLAY_PAUSE)) {
                    add(action("media.play", "Play", "play"))
                }
                if (has(PlaybackState.ACTION_SKIP_TO_NEXT)) add(action("media.next", "Next", "next"))
                if (has(PlaybackState.ACTION_STOP)) add(action("media.stop", "Stop", "stop"))
                state.customActions.forEach { ca ->
                    add(action("media.custom.${ca.action}", ca.name?.toString() ?: ca.action))
                }
                if (c.sessionActivity != null) add(action("open", "Open"))
            })
        }
        orbit.publisher.submit(activity, newArts)
    }

    // --- actions ---

    override fun perform(activity: JsonObject, actionId: String, value: JsonElement?, done: (Boolean, String?) -> Unit) {
        val id = activity.str("id") ?: return done(false, "bad activity")
        handler.post {
            val result = runCatching {
                if (id.startsWith("media:")) performMedia(id, actionId, value) else performNotification(id, actionId, value)
            }.getOrElse { e -> Log.w(TAG, "action $actionId on $id failed", e); e.message ?: e.toString() }
            done(result == null, result)
        }
    }

    /** Returns null on success, else an error string. */
    private fun performMedia(id: String, actionId: String, value: JsonElement?): String? {
        val c = controllers[id] ?: return "media session gone"
        val tc = c.transportControls
        when {
            actionId == "media.play" -> tc.play()
            actionId == "media.pause" -> tc.pause()
            actionId == "media.next" -> tc.skipToNext()
            actionId == "media.previous" -> tc.skipToPrevious()
            actionId == "media.stop" -> tc.stop()
            actionId == "media.seek" -> {
                val ms = (value as? JsonPrimitive)?.content?.toDoubleOrNull()?.toLong() ?: return "seek needs a position"
                tc.seekTo(ms.coerceAtLeast(0))
            }
            actionId.startsWith("media.custom.") -> {
                val name = actionId.removePrefix("media.custom.")
                val ca = c.playbackState?.customActions?.firstOrNull { it.action == name } ?: return "custom action gone"
                tc.sendCustomAction(ca, ca.extras)
            }
            actionId == "open" -> return sendIntent(c.sessionActivity ?: return "no session activity")
            else -> return "unsupported action"
        }
        return null
    }

    private fun performNotification(id: String, actionId: String, value: JsonElement?): String? {
        val sbn = notifs[id] ?: return "notification gone"
        val n = sbn.notification
        return when {
            actionId == "open" -> sendIntent(n.contentIntent ?: return "no content intent")
            actionId == "dismiss" -> { cancelNotification(sbn.key); null }
            actionId.startsWith("notif.") -> {
                val a = n.actions?.getOrNull(actionId.removePrefix("notif.").toIntOrNull() ?: -1) ?: return "action gone"
                val text = (value as? JsonPrimitive)?.takeIf { it.isString }?.content
                val inputs = a.remoteInputs
                if (text != null && !inputs.isNullOrEmpty()) {
                    val fill = Intent()
                    RemoteInput.addResultsToIntent(inputs, fill, Bundle().apply { inputs.forEach { putCharSequence(it.resultKey, text) } })
                    sendIntent(a.actionIntent, fill)
                } else {
                    sendIntent(a.actionIntent)
                }
            }
            else -> "unsupported action"
        }
    }

    private fun sendIntent(pi: PendingIntent, fill: Intent? = null): String? = try {
        val options = if (Build.VERSION.SDK_INT >= 34) {
            ActivityOptions.makeBasic()
                .setPendingIntentBackgroundActivityStartMode(ActivityOptions.MODE_BACKGROUND_ACTIVITY_START_ALLOWED)
                .toBundle()
        } else {
            null
        }
        pi.send(this, 0, fill, null, null, null, options)
        null
    } catch (e: PendingIntent.CanceledException) {
        "intent cancelled"
    }

    // --- helpers ---

    private fun appName(pkg: String): String = runCatching {
        @Suppress("DEPRECATION")
        packageManager.getApplicationLabel(packageManager.getApplicationInfo(pkg, 0)).toString()
    }.getOrDefault(pkg)

    private fun action(id: String, label: String, icon: String? = iconFor(label), extra: JsonObjectBuilder.() -> Unit = {}) =
        buildJsonObject {
            put("id", id)
            put("label", label)
            icon?.let { put("icon", it) }
            extra()
        }

    companion object {
        /** Notification.FLAG_PROMOTED_ONGOING (API 36). */
        private const val FLAG_PROMOTED_ONGOING = 0x00040000
        /** Notification.CATEGORY_STOPWATCH, added in API 31. */
        private const val CATEGORY_STOPWATCH = "stopwatch"
        private val EXCLUDED_PACKAGES = setOf("android", "com.android.systemui")
        private val LIVE_CATEGORIES = setOf(
            Notification.CATEGORY_TRANSPORT, Notification.CATEGORY_PROGRESS, Notification.CATEGORY_NAVIGATION,
            Notification.CATEGORY_CALL, CATEGORY_STOPWATCH, Notification.CATEGORY_ALARM,
        )
        private val ICONS = listOf(
            "pause" to "pause", "play" to "play", "resume" to "play", "next" to "next", "skip" to "next",
            "previous" to "previous", "stop" to "stop", "reply" to "reply", "hang up" to "call_end",
            "end call" to "call_end", "decline" to "call_end", "mute" to "mute", "unmute" to "unmute", "dismiss" to "close",
            "cancel" to "close", "like" to "like", "dislike" to "dislike", "shuffle" to "shuffle", "repeat" to "repeat",
            "add" to "add",
        )

        /** Well-known icon (spec 5.2) for a label, matched on its first word. */
        fun iconFor(label: String): String? {
            val l = label.trim().lowercase()
            return ICONS.firstOrNull { (k, _) -> l == k || l.startsWith("$k ") }?.second
        }
    }
}

@Suppress("DEPRECATION")
private inline fun <reified T : android.os.Parcelable> Bundle.parcelable(key: String): T? =
    if (Build.VERSION.SDK_INT >= 33) getParcelable(key, T::class.java) else getParcelable(key) as? T
