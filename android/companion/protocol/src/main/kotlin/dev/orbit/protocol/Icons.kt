package dev.orbit.protocol

/** Mapping of source actions to the well-known Action `icon` values (spec 5.2). */
object Icons {
    private val LABELS = listOf(
        "pause" to "pause", "play" to "play", "resume" to "play", "next" to "next", "skip" to "next",
        "previous" to "previous", "stop" to "stop", "reply" to "reply", "hang up" to "call_end",
        "end call" to "call_end", "decline" to "call_end", "unmute" to "unmute", "mute" to "mute",
        "dismiss" to "close", "cancel" to "close", "close" to "close", "like" to "like", "dislike" to "dislike",
        "shuffle" to "shuffle", "repeat" to "repeat", "add" to "add", "remove" to "remove",
        "done" to "check", "mark as read" to "check", "complete" to "check",
    )

    /** Icon for a label, matched on its leading words ("Pause", "Mark as read", "Hang up"). */
    fun forLabel(label: String): String? {
        val l = label.trim().lowercase()
        return LABELS.firstOrNull { (k, _) -> l == k || l.startsWith("$k ") }?.second
    }

    /**
     * Icon for a media session custom action. Keyed on the action id first, so toggles whose label
     * flips ("Like" / "Remove like", "Shuffle on" / "Shuffle off") keep a stable icon.
     */
    fun forCustomAction(action: String, label: String): String? {
        val tokens = action.uppercase().split('_', '.', '-', ' ', ':', '/').filter { it.isNotEmpty() }.toSet()
        fun has(vararg t: String) = t.any { it in tokens }
        return when {
            has("DISLIKE", "THUMBSDOWN") || action.uppercase().contains("THUMBS_DOWN") -> "dislike"
            has("LIKE", "HEART", "FAVORITE", "FAVOURITE", "THUMBSUP") || action.uppercase().contains("THUMBS_UP") -> "like"
            has("SHUFFLE") -> "shuffle"
            has("REPEAT", "LOOP") -> "repeat"
            has("PLAYLIST", "ADD", "SAVE", "LIBRARY") -> "add"
            else -> forLabel(label)
        }
    }
}
