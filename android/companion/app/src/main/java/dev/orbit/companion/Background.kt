package dev.orbit.companion

import android.annotation.SuppressLint
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.PowerManager
import android.provider.Settings

/** Settings that keep Orbit running in the background: battery optimization and OEM app killers. */
object Background {
    fun ignoringBatteryOptimizations(context: Context): Boolean =
        context.getSystemService(PowerManager::class.java).isIgnoringBatteryOptimizations(context.packageName)

    @SuppressLint("BatteryLife") // keeping links to computers alive is the app's purpose
    fun batteryExemptionIntent(context: Context): Intent =
        Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:${context.packageName}"))
            .takeIf { it.resolveActivity(context.packageManager) != null }
            ?: Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS)

    /** OEMs whose own background managers stop apps beyond what Android does. */
    fun oemBrand(manufacturer: String): String? = when (manufacturer.trim().lowercase()) {
        "oppo" -> "OPPO"
        "realme" -> "realme"
        "oneplus" -> "OnePlus"
        "xiaomi", "redmi", "poco" -> "Xiaomi"
        "huawei", "honor" -> "Huawei"
        "samsung" -> "Samsung"
        else -> null
    }

    /** The OEM's auto-start / background screen if one is reachable, else Orbit's app details. */
    fun oemSettingsIntent(context: Context, brand: String): Intent {
        val pm = context.packageManager
        OEM_COMPONENTS[brand].orEmpty().forEach { (pkg, cls) ->
            val intent = Intent().setComponent(ComponentName(pkg, cls)).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            val info = runCatching { pm.getActivityInfo(intent.component!!, 0) }.getOrNull()
            if (info != null && info.exported && info.enabled) return intent
        }
        return Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:${context.packageName}"))
    }

    private val COLOROS = listOf(
        "com.coloros.safecenter" to "com.coloros.safecenter.startupapp.StartupAppListActivity",
        "com.coloros.safecenter" to "com.coloros.safecenter.permission.startup.StartupAppListActivity",
        "com.oplus.safecenter" to "com.oplus.safecenter.startupapp.StartupAppListActivity",
        "com.oppo.safe" to "com.oppo.safe.permission.startup.StartupAppListActivity",
    )

    /** Kept in sync with the `<queries>` packages in the manifest. */
    private val OEM_COMPONENTS = mapOf(
        "OPPO" to COLOROS,
        "realme" to COLOROS,
        "OnePlus" to COLOROS + ("com.oneplus.security" to "com.oneplus.security.chainlaunch.view.ChainLaunchAppListActivity"),
        "Xiaomi" to listOf("com.miui.securitycenter" to "com.miui.permcenter.autostart.AutoStartManagementActivity"),
        "Huawei" to listOf(
            "com.huawei.systemmanager" to "com.huawei.systemmanager.startupmgr.ui.StartupNormalAppListActivity",
            "com.huawei.systemmanager" to "com.huawei.systemmanager.optimize.process.ProtectActivity",
        ),
        "Samsung" to listOf(
            "com.samsung.android.lool" to "com.samsung.android.sm.battery.ui.BatteryActivity",
            "com.samsung.android.sm" to "com.samsung.android.sm.ui.battery.BatteryActivity",
        ),
    )
}
