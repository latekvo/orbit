package dev.orbit.companion

import android.content.Context
import android.os.Build
import android.provider.Settings

/** Picks the human device name shown to computers (hello `name`, DNS-SD instance name). */
object DeviceNames {
    private val ANDROID_HOST = Regex("^android[-_][0-9a-f]+$", RegexOption.IGNORE_CASE)
    private val RANDOM_TOKEN = Regex("^(?=.*[0-9])(?=.*[a-z])[a-z0-9]{4,10}$")
    private val SNAKE_ID = Regex("^[a-z0-9]+(_[a-z0-9]+)+$")

    /**
     * True for names nobody typed: blank, a build identifier, an `android-<hex>` hostname, or a short
     * lowercase letters+digits token (ColorOS seeds DEVICE_NAME and bluetooth_name with e.g. "gv0iw").
     */
    fun looksGenerated(name: String, buildIds: Collection<String>): Boolean {
        val n = name.trim()
        return n.isEmpty() || n.equals("android", ignoreCase = true) ||
            buildIds.any { it.isNotBlank() && n.equals(it, ignoreCase = true) } ||
            ANDROID_HOST.matches(n) || RANDOM_TOKEN.matches(n) || SNAKE_ID.matches(n)
    }

    /** "OPPO CPH2043", "Google Pixel 8" (not "Google Google Pixel 8"), "Samsung SM-S918B". */
    fun pretty(manufacturer: String, model: String): String {
        val m = model.trim()
        val brand = manufacturer.trim().replaceFirstChar { it.titlecase() }
        return when {
            brand.isEmpty() -> m
            m.isEmpty() -> brand
            m.startsWith(brand, ignoreCase = true) -> m
            else -> "$brand $m"
        }
    }

    /** The first candidate that looks user-set, else [pretty] make and model. */
    fun choose(candidates: List<String?>, manufacturer: String, model: String, buildIds: Collection<String>): String =
        candidates.firstNotNullOfOrNull { c -> c?.trim()?.takeUnless { looksGenerated(it, buildIds) } }
            ?: pretty(manufacturer, model)

    /** System-provided name, without a user override. */
    fun system(context: Context): String {
        val cr = context.contentResolver
        fun read(get: () -> String?) = try { get() } catch (_: RuntimeException) { null }
        val candidates = listOf(
            read { Settings.Global.getString(cr, Settings.Global.DEVICE_NAME) },
            read { Settings.Secure.getString(cr, "bluetooth_name") },
            // Vendor keys some skins (OnePlus/OxygenOS) keep the About-phone name in.
            read { Settings.System.getString(cr, "oem_oneplus_devicename") },
        )
        return choose(candidates, Build.MANUFACTURER, Build.MODEL, listOf(Build.MODEL, Build.DEVICE, Build.PRODUCT))
    }
}
