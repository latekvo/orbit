package dev.orbit.protocol

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class IconsTest {
    @Test
    fun customActionIconsAreIdFirstAndStableAcrossLabelFlips() {
        // Metrolist's custom actions, with both label states.
        for (label in listOf("Like", "Remove like")) assertEquals("like", Icons.forCustomAction("TOGGLE_LIKE", label))
        for (label in listOf("Shuffle on", "Shuffle off")) assertEquals("shuffle", Icons.forCustomAction("TOGGLE_SHUFFLE", label))
        for (label in listOf("Repeat mode off", "Repeat mode one", "Repeat mode all")) {
            assertEquals("repeat", Icons.forCustomAction("TOGGLE_REPEAT_MODE", label))
        }
        assertEquals("add", Icons.forCustomAction("ADD_TO_TARGET_PLAYLIST", "Quick-add destination"))
        assertNull(Icons.forCustomAction("TOGGLE_START_RADIO", "Start radio"))
        assertEquals("dislike", Icons.forCustomAction("THUMBS_DOWN", "Like")) // id wins over label
        assertEquals("dislike", Icons.forCustomAction("com.app.DISLIKE", "Not for me"))
        assertEquals("like", Icons.forCustomAction("com.app.action.HEART", "Love"))
        assertEquals("like", Icons.forCustomAction("FAVORITE", "Favourite"))
        // Unknown ids fall back to the label.
        assertEquals("stop", Icons.forCustomAction("X1", "Stop"))
    }

    @Test
    fun notificationLabels() {
        val cases = mapOf(
            "Pause" to "pause", "Resume" to "play", "Play" to "play", "Cancel" to "close", "Reply" to "reply",
            "Stop" to "stop", "Hang up" to "call_end", "Decline" to "call_end", "Mute" to "mute", "Unmute" to "unmute",
            "Mark as read" to "check", "Dismiss" to "close", "Next" to "next", "Skip ad" to "next",
            "+1 min" to null, "Lap" to null, "Start radio" to null, "Muted" to null, "Remove like" to "remove",
        )
        for ((label, icon) in cases) assertEquals(label, icon, Icons.forLabel(label))
    }
}
