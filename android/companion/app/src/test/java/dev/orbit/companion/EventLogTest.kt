package dev.orbit.companion

import org.junit.Assert.assertEquals
import org.junit.Test

class EventLogTest {
    @Test
    fun exitReasonsOfNewerReleasesHaveNames() {
        assertEquals("freezer", EventLog.exitReason(14))
        assertEquals("package_state_change", EventLog.exitReason(15))
        assertEquals("package_updated", EventLog.exitReason(16))
        assertEquals("reason 99", EventLog.exitReason(99))
    }
}
