package dev.orbit.companion

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject

/** A release version: `X.Y.Z` with an optional leading `v`, nothing else. */
data class Version(val major: Int, val minor: Int, val patch: Int) : Comparable<Version> {
    override fun compareTo(other: Version) = compareValuesBy(this, other, Version::major, Version::minor, Version::patch)

    override fun toString() = "$major.$minor.$patch"

    companion object {
        private val PATTERN = Regex("""v?(\d{1,9})\.(\d{1,9})\.(\d{1,9})""")

        fun parse(text: String): Version? = PATTERN.matchEntire(text.trim())?.groupValues?.let {
            Version(it[1].toInt(), it[2].toInt(), it[3].toInt())
        }
    }
}

/** A GitHub release; [apkUrl] is its first `.apk` asset, if any. */
data class Release(val version: Version, val apkUrl: String?, val pageUrl: String)

object Releases {
    private val json = Json { ignoreUnknownKeys = true }

    /** Parses a `releases/latest` response; null for drafts, pre-releases and unversioned tags. */
    fun parse(body: String): Release? {
        val o = runCatching { json.parseToJsonElement(body).jsonObject }.getOrNull() ?: return null
        if (o.flag("draft") || o.flag("prerelease")) return null
        val version = o.text("tag_name")?.let(Version::parse) ?: return null
        val apk = (o["assets"] as? JsonArray).orEmpty().mapNotNull { it as? JsonObject }
            .firstOrNull { it.text("name")?.endsWith(".apk", ignoreCase = true) == true }
            ?.text("browser_download_url")
        return Release(version, apk, o.text("html_url") ?: "")
    }

    /** The release to offer: newer than [installed], has an APK, and is not at or below [dismissed]. */
    fun offer(installed: String, release: Release?, dismissed: String?): Release? {
        val current = Version.parse(installed) ?: return null
        if (release?.apkUrl == null || release.version <= current) return null
        if (dismissed?.let(Version::parse)?.let { release.version <= it } == true) return null
        return release
    }

    /** Whether a check last made at [lastMs] is [intervalMs] old; a clock set back counts as due. */
    fun due(nowMs: Long, lastMs: Long, intervalMs: Long) = nowMs - lastMs !in 0 until intervalMs

    private fun JsonObject.text(key: String) = (this[key] as? JsonPrimitive)?.takeIf { it.isString }?.content

    private fun JsonObject.flag(key: String) = (this[key] as? JsonPrimitive)?.content == "true"
}
