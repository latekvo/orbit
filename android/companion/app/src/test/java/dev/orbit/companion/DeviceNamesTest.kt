package dev.orbit.companion

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class DeviceNamesTest {
    private val oppoIds = listOf("CPH2043", "OP4BA2L1", "CPH2043EEA")

    @Test
    fun oppoWithSeededRandomNamesFallsBackToMakeAndModel() {
        // Captured from the OPPO CPH2043: both system name keys hold "gv0iw".
        assertEquals("OPPO CPH2043", DeviceNames.choose(listOf("gv0iw", "gv0iw", null), "OPPO", "CPH2043", oppoIds))
    }

    @Test
    fun userSetNamesWinInOrder() {
        assertEquals("Ignacy's phone", DeviceNames.choose(listOf("Ignacy's phone", "BT name"), "OPPO", "CPH2043", oppoIds))
        assertEquals("Reno BT", DeviceNames.choose(listOf("gv0iw", "Reno BT"), "OPPO", "CPH2043", oppoIds))
        assertEquals("Pixel", DeviceNames.choose(listOf("  ", " Pixel "), "Google", "Pixel 8", listOf("Pixel 8")))
    }

    @Test
    fun emulatorModelIsNotAUserName() {
        val ids = listOf("sdk_gphone64_arm64", "emu64a", "sdk_gphone64_arm64")
        assertEquals("Google sdk_gphone64_arm64", DeviceNames.choose(listOf("sdk_gphone64_arm64", null), "Google", "sdk_gphone64_arm64", ids))
    }

    @Test
    fun generatedNameHeuristics() {
        for (n in listOf("", "Android", "android-1a2b3c4d5e6f", "gv0iw", "a1b2c3", "sdk_gphone64_arm64", "CPH2043")) {
            assertTrue(n, DeviceNames.looksGenerated(n, oppoIds))
        }
        for (n in listOf("Ignacy's phone", "Pixel 8", "OPPO Reno", "kitchen", "Galaxy")) {
            assertFalse(n, DeviceNames.looksGenerated(n, oppoIds))
        }
    }

    @Test
    fun prettyDoesNotDoubleTheManufacturer() {
        assertEquals("OPPO CPH2043", DeviceNames.pretty("OPPO", "CPH2043"))
        assertEquals("Google Pixel 8", DeviceNames.pretty("Google", "Pixel 8"))
        assertEquals("Google Pixel 8", DeviceNames.pretty("Google", "Google Pixel 8"))
        assertEquals("Samsung SM-S918B", DeviceNames.pretty("samsung", "SM-S918B"))
        assertEquals("CPH2043", DeviceNames.pretty("", "CPH2043"))
    }
}
