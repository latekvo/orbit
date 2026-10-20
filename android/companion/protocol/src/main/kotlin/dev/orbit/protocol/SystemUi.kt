package dev.orbit.protocol

/**
 * Which actions the phone's own UI renders as buttons, per AOSP: Notification.Builder for
 * notifications, SystemUI MediaDataManager for the media player. Everything else is `overflow`
 * (spec 5.2).
 */
object SystemUi {
    /** Notification.MAX_ACTION_BUTTONS (API 29-35). */
    const val NOTIFICATION_BUTTONS = 3

    /** Media player slots: MediaStyle.MAX_MEDIA_BUTTONS and SystemUI's five generic action buttons. */
    const val MEDIA_BUTTONS = 5

    /** androidx MediaConstants.SESSION_EXTRAS_KEY_SLOT_RESERVATION_SKIP_TO_PREV / _NEXT. */
    const val RESERVE_PREV = "android.media.playback.ALWAYS_RESERVE_SPACE_FOR.ACTION_SKIP_TO_PREVIOUS"
    const val RESERVE_NEXT = "android.media.playback.ALWAYS_RESERVE_SPACE_FOR.ACTION_SKIP_TO_NEXT"

    data class NotifAction(
        val title: String?,
        val hasIcon: Boolean = true,
        val hasIntent: Boolean = true,
        val contextual: Boolean = false,
    )

    /**
     * Per notification action: null = dropped, false = button, true = overflow.
     * Standard template: non-contextual actions fill [NOTIFICATION_BUTTONS] slots in order.
     * MediaStyle template: the first [MEDIA_BUTTONS] actions are buttons, those without an icon skipped.
     * Actions without an intent can't be performed and are dropped, but still take their slot
     * (the phone shows them disabled).
     */
    fun notificationButtons(actions: List<NotifAction>, mediaStyle: Boolean): List<Boolean?> {
        var slots = NOTIFICATION_BUTTONS
        return actions.mapIndexed { i, a ->
            val button = when {
                mediaStyle -> i < MEDIA_BUTTONS && a.hasIcon
                a.contextual -> false
                slots > 0 -> { slots--; true }
                else -> false
            }
            if (a.hasIntent) !button else null
        }
    }

    /**
     * SystemUI on API 33+ for apps targeting 33+ (MediaDataManager.createActionsFromState):
     * play/pause, then the previous slot (previous, else a custom action unless reserved), the
     * next slot likewise, then two more custom actions. Stop is never shown.
     */
    fun sessionButtons(
        playPause: String?,
        previous: String?,
        next: String?,
        custom: List<String>,
        reservePrev: Boolean,
        reserveNext: Boolean,
    ): Set<String> {
        val queue = custom.iterator()
        fun nextCustom() = if (queue.hasNext()) queue.next() else null
        return setOfNotNull(
            playPause,
            previous ?: if (!reservePrev) nextCustom() else null,
            next ?: if (!reserveNext) nextCustom() else null,
            nextCustom(),
            nextCustom(),
        )
    }

    /**
     * The player built from the MediaStyle notification (API < 33, or apps targeting < 33): its
     * visible actions mapped onto session action ids. Custom actions match by name; transport
     * actions by label keyword, else by order among the remaining [transport] ids.
     *
     * @param transport session transport ids in display order, e.g. media.previous, media.pause, media.next.
     * @param custom session custom actions as (id, name).
     */
    fun notificationMediaButtons(
        notification: List<NotifAction>,
        transport: List<String>,
        custom: List<Pair<String, String>>,
    ): Set<String> {
        val visible = notification.zip(notificationButtons(notification, mediaStyle = true))
            .filter { it.second == false }.map { it.first.title?.trim().orEmpty() }
        val customLeft = custom.toMutableList()
        val transportLeft = transport.toMutableList()
        val unmatched = mutableListOf<String>()
        val result = linkedSetOf<String>()
        for (title in visible) {
            val c = customLeft.firstOrNull { it.second.trim().equals(title, ignoreCase = true) }
            if (c != null) {
                customLeft.remove(c)
                result += c.first
                continue
            }
            val t = transportFor(title)?.let { kw -> transportLeft.firstOrNull { it in kw } }
            if (t != null) {
                transportLeft.remove(t)
                result += t
            } else {
                unmatched += title
            }
        }
        unmatched.zip(transportLeft).forEach { result += it.second }
        return result
    }

    private val WORD = Regex("[a-z]+")

    private fun transportFor(title: String): Set<String>? {
        val words = WORD.findAll(title.lowercase()).map { it.value }.toSet()
        return when {
            words.any { it == "previous" || it == "prev" } -> setOf("media.previous")
            "next" in words -> setOf("media.next")
            words.any { it == "pause" || it == "play" || it == "resume" } -> setOf("media.pause", "media.play")
            "stop" in words -> setOf("media.stop")
            else -> null
        }
    }
}
