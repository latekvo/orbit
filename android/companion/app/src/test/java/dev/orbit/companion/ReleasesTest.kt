package dev.orbit.companion

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ReleasesTest {
    private fun v(text: String) = Version.parse(text)!!

    @Test
    fun versionsCompareNumerically() {
        assertTrue(v("0.10.0") > v("0.9.0"))
        assertTrue(v("1.0.0") > v("0.99.99"))
        assertTrue(v("0.2.10") > v("0.2.9"))
        assertTrue(v("0.3.0") > v("0.2.9"))
        assertEquals(0, v("0.2.0").compareTo(v("v0.2.0")))
        assertEquals(Version(0, 2, 0), v("v0.2.0"))
    }

    @Test
    fun garbageVersionsAreRejected() {
        for (tag in listOf("", "v", "latest", "0.2", "0.2.0.1", "v0.2.0-rc1", "0.2.0-beta", "V0.2.0", "vv0.2.0", "0.x.0", "-1.0.0", "1.0.99999999999")) {
            assertNull(tag, Version.parse(tag))
        }
    }

    private fun release(tag: String = "v0.2.0", assets: String = APK_ASSETS, extra: String = "") = """
        {"tag_name": "$tag", "html_url": "https://github.com/latekvo/orbit/releases/tag/$tag",
         "draft": false, "prerelease": false, $extra "assets": $assets}
    """

    @Test
    fun parsePicksTheApkAsset() {
        val r = Releases.parse(release())!!
        assertEquals(Version(0, 2, 0), r.version)
        assertEquals("https://example.test/orbit-companion-0.2.0.apk", r.apkUrl)
        assertEquals("https://github.com/latekvo/orbit/releases/tag/v0.2.0", r.pageUrl)
    }

    @Test
    fun parseWithoutApkGivesNoUpdate() {
        val other = """[{"name": "orbit-0.2.0.tar.gz", "browser_download_url": "https://example.test/orbit-0.2.0.tar.gz"},
                        {"name": "apk-notes.txt", "browser_download_url": "https://example.test/apk-notes.txt"}]"""
        for (assets in listOf(other, "[]")) {
            val r = Releases.parse(release(assets = assets))!!
            assertNull(r.apkUrl)
            assertNull(Releases.offer("0.1.0", r, null))
        }
        val noAssets = Releases.parse("""{"tag_name": "v0.2.0", "html_url": "https://x.test"}""")!!
        assertNull(noAssets.apkUrl)
        assertNull(Releases.offer("0.1.0", noAssets, null))
    }

    @Test
    fun parseRejectsUnusableReleases() {
        assertNull(Releases.parse(release(tag = "nightly")))
        assertNull(Releases.parse(release().replace("\"draft\": false", "\"draft\": true")))
        assertNull(Releases.parse(release().replace("\"prerelease\": false", "\"prerelease\": true")))
        assertNull(Releases.parse("not json"))
        assertNull(Releases.parse("[]"))
    }

    @Test
    fun offerOnlyNewerVersions() {
        val r = Releases.parse(release())!!
        assertEquals(r, Releases.offer("0.1.0", r, null))
        assertNull(Releases.offer("0.2.0", r, null))
        assertNull(Releases.offer("0.3.0", r, null))
        assertNull(Releases.offer("garbage", r, null))
        assertNull(Releases.offer("0.1.0", null, null))
    }

    @Test
    fun notNowHidesThatVersionUntilANewerOne() {
        val r020 = Releases.parse(release())!!
        val r030 = Releases.parse(release(tag = "v0.3.0"))!!
        assertNull(Releases.offer("0.1.0", r020, "0.2.0"))
        assertEquals(r030, Releases.offer("0.1.0", r030, "0.2.0"))
        assertNull(Releases.offer("0.1.0", r020, "0.3.0"))
        assertEquals(r020, Releases.offer("0.1.0", r020, "0.1.0"))
    }

    @Test
    fun dueAfterTheInterval() {
        val hour = 3_600_000L
        assertTrue(Releases.due(nowMs = 10 * hour, lastMs = 0, intervalMs = hour))
        assertFalse(Releases.due(nowMs = 10 * hour, lastMs = 10 * hour - 1, intervalMs = hour))
        assertFalse(Releases.due(nowMs = 10 * hour, lastMs = 10 * hour, intervalMs = hour))
        assertTrue(Releases.due(nowMs = 10 * hour, lastMs = 9 * hour, intervalMs = hour))
        assertTrue(Releases.due(nowMs = 10 * hour, lastMs = 11 * hour, intervalMs = hour))
    }

    private companion object {
        const val APK_ASSETS = """[
            {"name": "orbit-0.2.0.tar.gz", "browser_download_url": "https://example.test/orbit-0.2.0.tar.gz"},
            {"name": "orbit-companion-0.2.0.apk", "browser_download_url": "https://example.test/orbit-companion-0.2.0.apk"},
            {"name": "orbit-companion-0.2.0-alt.apk", "browser_download_url": "https://example.test/alt.apk"}
        ]"""
    }
}
