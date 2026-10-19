package dev.orbit.protocol

/** Re-encodes images with the platform's codecs (Android: Bitmap), keeping this module pure Kotlin. */
fun interface ArtTranscoder {
    /** [art] scaled to fit [maxPx] x [maxPx], aiming at [maxBytes]; null when it cannot be decoded. */
    fun scale(art: Art, maxPx: Int, maxBytes: Int): Art?
}

/**
 * Picks the variant of each image a session sends: stored art on LAN, small variants on BLE,
 * where tens of KB take seconds. A variant is content-addressed like any art, so it has its own
 * key and never replaces the full image in a consumer's cache. Thread-safe; shared by sessions.
 */
class ArtSizer(private val transcoder: ArtTranscoder, private val capacity: Int = 64) {
    private val variants = object : LinkedHashMap<String, Art?>(16, 0.75f, true) {
        override fun removeEldestEntry(eldest: MutableMap.MutableEntry<String, Art?>) = size > capacity
    }

    /** What to send for [art], referenced by activity field [field], over [transport]; null omits it. */
    fun forTransport(art: Art, field: String, transport: String): Art? {
        if (transport != "ble") return art
        val (maxPx, maxBytes) = if (field == "iconKey") BLE_ICON else BLE_ART
        val id = "${art.key}/$maxPx"
        synchronized(variants) { if (variants.containsKey(id)) return variants[id] }
        val scaled = runCatching { transcoder.scale(art, maxPx, maxBytes) }.getOrNull()
        val pick = listOfNotNull(scaled, art).filter { it.data.size <= maxBytes }.minByOrNull { it.data.size }
        synchronized(variants) { variants[id] = pick }
        return pick
    }

    companion object {
        /** (max px, max bytes): ~8 KB of base64 for art, which the desktop shows at 56 px. */
        val BLE_ART = 128 to 6 * 1024
        /** Shown at 16 px. */
        val BLE_ICON = 64 to 4 * 1024
    }
}
