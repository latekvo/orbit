package dev.orbit.protocol

import dev.orbit.protocol.SystemUi.NotifAction
import org.junit.Assert.assertEquals
import org.junit.Test

class SystemUiTest {
    // Metrolist on the OPPO (API 31) as captured by dumpsys notification / media_session.
    private val metrolistNotification = listOf(
        "Seek to previous item", "Pause", "Seek to next item", "Remove like", "Repeat mode off",
        "Shuffle on", "Start radio", "Quick-add destination",
    ).map { NotifAction(it) }
    private val metrolistCustom = listOf(
        "media.custom.TOGGLE_LIKE" to "Remove like",
        "media.custom.TOGGLE_REPEAT_MODE" to "Repeat mode off",
        "media.custom.TOGGLE_SHUFFLE" to "Shuffle on",
        "media.custom.TOGGLE_START_RADIO" to "Start radio",
        "media.custom.ADD_TO_TARGET_PLAYLIST" to "Quick-add destination",
    )
    private val metrolistVisible = setOf(
        "media.previous", "media.pause", "media.next", "media.custom.TOGGLE_LIKE", "media.custom.TOGGLE_REPEAT_MODE",
    )

    @Test
    fun metrolistBelow33UsesTheFirstFiveNotificationActions() {
        val visible = SystemUi.notificationMediaButtons(
            metrolistNotification, listOf("media.previous", "media.pause", "media.next"), metrolistCustom,
        )
        assertEquals(metrolistVisible, visible)
    }

    @Test
    fun metrolistOn33UsesSessionSlots() {
        val visible = SystemUi.sessionButtons(
            "media.pause", "media.previous", "media.next", metrolistCustom.map { it.first },
            reservePrev = false, reserveNext = false,
        )
        assertEquals(metrolistVisible, visible)
    }

    @Test
    fun sessionSlotsFillMissingTransportWithCustomUnlessReserved() {
        val custom = listOf("c1", "c2", "c3", "c4", "c5")
        assertEquals(
            setOf("media.play", "c1", "media.next", "c2", "c3"),
            SystemUi.sessionButtons("media.play", null, "media.next", custom, reservePrev = false, reserveNext = false),
        )
        assertEquals(
            setOf("media.play", "media.next", "c1", "c2"),
            SystemUi.sessionButtons("media.play", null, "media.next", custom, reservePrev = true, reserveNext = false),
        )
        assertEquals(
            setOf("media.play", "media.previous", "c1", "c2"),
            SystemUi.sessionButtons("media.play", "media.previous", null, custom, reservePrev = false, reserveNext = true),
        )
        assertEquals(
            setOf("media.pause", "c1", "c2", "c3", "c4"),
            SystemUi.sessionButtons("media.pause", null, null, custom, reservePrev = false, reserveNext = false),
        )
    }

    @Test
    fun sampleProducerMedia() {
        val transport = listOf("media.previous", "media.pause", "media.next", "media.stop")
        // API 33+: stop is never a session button.
        assertEquals(
            setOf("media.previous", "media.pause", "media.next"),
            SystemUi.sessionButtons("media.pause", "media.previous", "media.next", emptyList(), false, false),
        )
        // Below 33 its MediaStyle notification shows all four, Stop included.
        val notification = listOf("Previous", "Pause", "Next", "Stop").map { NotifAction(it) }
        assertEquals(transport.toSet(), SystemUi.notificationMediaButtons(notification, transport, emptyList()))
    }

    @Test
    fun notificationMediaMatchesStaleLabelsAndFallsBackToOrder() {
        // The notification still says "Play" while the session already reports playing.
        val stale = listOf("Previous", "Play", "Next").map { NotifAction(it) }
        assertEquals(
            setOf("media.previous", "media.pause", "media.next"),
            SystemUi.notificationMediaButtons(stale, listOf("media.previous", "media.pause", "media.next"), emptyList()),
        )
        val paused = listOf("Previous", "Pause", "Next").map { NotifAction(it) }
        assertEquals(
            setOf("media.previous", "media.play", "media.next"),
            SystemUi.notificationMediaButtons(paused, listOf("media.previous", "media.play", "media.next", "media.stop"), emptyList()),
        )
        assertEquals(
            setOf("media.stop", "media.play"),
            SystemUi.notificationMediaButtons(
                listOf(NotifAction("Stop"), NotifAction("Pause")), listOf("media.previous", "media.play", "media.next", "media.stop"), emptyList(),
            ),
        )
        // Labels in another language: transport ids are matched by order.
        val polish = listOf("Poprzedni", "Wstrzymaj", "Dalej", "Polub").map { NotifAction(it) }
        assertEquals(
            setOf("media.previous", "media.pause", "media.next", "media.custom.LIKE"),
            SystemUi.notificationMediaButtons(
                polish, listOf("media.previous", "media.pause", "media.next", "media.stop"), listOf("media.custom.LIKE" to "Polub"),
            ),
        )
    }

    @Test
    fun mediaStyleSkipsIconlessActionsWithinTheFirstFive() {
        val actions = listOf(
            NotifAction("Previous"), NotifAction("Pause", hasIcon = false), NotifAction("Next"),
            NotifAction("A"), NotifAction("B"), NotifAction("C"),
        )
        assertEquals(listOf(false, true, false, false, false, true), SystemUi.notificationButtons(actions, mediaStyle = true))
    }

    @Test
    fun genericNotificationShowsThreeButtons() {
        val five = (1..5).map { NotifAction("Action $it") }
        assertEquals(listOf(false, false, false, true, true), SystemUi.notificationButtons(five, mediaStyle = false))
    }

    @Test
    fun contextualActionsOverflowAndIntentlessActionsAreDropped() {
        val actions = listOf(
            NotifAction("Smart reply", contextual = true),
            NotifAction("Archive"),
            NotifAction("Broken", hasIntent = false),
            NotifAction("Reply"),
            NotifAction("Mark as read"),
            NotifAction("Snooze"),
        )
        assertEquals(listOf(true, false, null, false, false, true), SystemUi.notificationButtons(actions, mediaStyle = false))
    }

    @Test
    fun sampleProducerNotifications() {
        // Download: Pause + Cancel; timer: Pause + "+1 min"; reply: one RemoteInput action. All fit.
        for (labels in listOf(listOf("Pause", "Cancel"), listOf("Pause", "+1 min"), listOf("Reply"))) {
            assertEquals(labels.map { false }, SystemUi.notificationButtons(labels.map { NotifAction(it) }, mediaStyle = false))
        }
    }
}
