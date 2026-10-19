package dev.orbit.protocol

import kotlinx.serialization.json.JsonObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test
import java.util.concurrent.ScheduledThreadPoolExecutor

class ActivityPublisherTest {
    private var now = 1_000_000L
    private val scheduler = ScheduledThreadPoolExecutor(1)
    private val hub = ActivityHub()
    private val events = mutableListOf<String>()
    private val publisher = ActivityPublisher(hub, scheduler, clock = { now }, debounceMs = 150, churnIntervalMs = 1000)

    init {
        hub.subscribe(object : ActivityHub.Listener {
            override fun onUpsert(activity: JsonObject) { events += "upsert:${activity.str("title")}" }
            override fun onRemove(id: String) { events += "remove:$id" }
        }) {}
    }

    private fun act(title: String, progress: Int = 0) =
        json("""{"id":"a","title":"$title","progress":{"value":$progress,"max":100,"indeterminate":false}}""")

    @Test
    fun burstIsCoalescedIntoOneUpdate() {
        publisher.submit(act("one"))
        publisher.submit(act("two"))
        publisher.submit(act("three"))
        publisher.flush()
        assertEquals(listOf("upsert:three"), events)
        assertEquals(now, hub.get("a")!!.long("updatedMs"))
        // Identical resubmission publishes nothing.
        now += 5000
        publisher.submit(act("three"))
        publisher.flush()
        assertEquals(1, events.size)
    }

    @Test
    fun progressOnlyChurnIsRateLimitedButRealChangesAreNot() {
        publisher.submit(act("t", 1)); publisher.flush()
        now += 200
        publisher.submit(act("t", 2)); publisher.flush()
        assertEquals(1, events.size) // held back
        now += 200
        publisher.submit(act("renamed", 3)); publisher.flush()
        assertEquals(listOf("upsert:t", "upsert:renamed"), events)
        now += 300
        publisher.submit(act("renamed", 4)); publisher.flush()
        assertEquals(2, events.size)
        now += 800
        publisher.flush()
        assertEquals(3, events.size)
        assertEquals(4L, (hub.get("a")!!["progress"] as JsonObject).long("value"))
    }

    @Test
    fun removeWinsOverPendingUpsert() {
        publisher.submit(act("x")); publisher.flush()
        publisher.submit(act("y"))
        publisher.remove("a")
        publisher.flush()
        assertEquals(listOf("upsert:x", "remove:a"), events)
        assertNull(hub.get("a"))
    }

    @Test
    fun consistentMediaPositionIsNotRepublished() {
        fun media(pos: Long, at: Long, state: String = "playing") =
            json("""{"id":"m","title":"S","media":{"state":"$state","positionMs":$pos,"$POSITION_AT":$at,"rate":1.0}}""")
        publisher.submit(media(1000, now)); publisher.flush()
        now += 5000
        publisher.submit(media(6000, now)); publisher.flush() // just playback progressing
        assertEquals(1, events.size)
        publisher.submit(media(60000, now)); publisher.flush() // a seek, but within the churn window
        now += 1000
        publisher.flush()
        assertEquals(2, events.size)
    }

    @Test
    fun wireFormExtrapolatesPlayingPositionAndStripsInternalField() {
        val a = json("""{"id":"m","media":{"state":"playing","positionMs":1000,"$POSITION_AT":5000,"rate":2.0,"durationMs":100000}}""")
        assertEquals(json("""{"id":"m","media":{"state":"playing","positionMs":3000,"rate":2.0,"durationMs":100000}}"""), activityForWire(a, 6000))
        val paused = json("""{"id":"m","media":{"state":"paused","positionMs":1000,"$POSITION_AT":5000}}""")
        assertEquals(json("""{"id":"m","media":{"state":"paused","positionMs":1000}}"""), activityForWire(paused, 9000))
    }
}
