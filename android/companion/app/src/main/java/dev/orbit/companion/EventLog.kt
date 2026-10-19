package dev.orbit.companion

import android.app.ActivityManager
import android.app.ApplicationExitInfo
import android.content.Context
import android.os.Build
import android.os.Process
import android.util.Log
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Rolling diagnostics log in files/events.log, shown in the app and readable with
 * `adb shell run-as dev.orbit.companion cat files/events.log`.
 */
object EventLog {
    private const val MAX_BYTES = 256 * 1024L
    private val lock = Any()
    private val time = SimpleDateFormat("yyyy-MM-dd HH:mm:ss.SSS", Locale.US)
    @Volatile private var file: File? = null

    fun init(context: Context) {
        file = File(context.filesDir, "events.log")
    }

    fun log(event: String) {
        Log.i(TAG, "event: $event")
        val f = file ?: return
        synchronized(lock) {
            try {
                if (f.length() > MAX_BYTES) {
                    val lines = f.readLines()
                    f.writeText(lines.drop(lines.size / 2).joinToString("\n", postfix = "\n"))
                }
                f.appendText("${time.format(Date())} [${Process.myPid()}] $event\n")
            } catch (e: java.io.IOException) {
                Log.w(TAG, "event log write failed", e)
            }
        }
    }

    fun read(): String = synchronized(lock) {
        file?.takeIf { it.exists() }?.readText().orEmpty()
    }

    /** Logs this process's start with how the previous one ended (API 30+). */
    fun logProcessStart(context: Context) {
        val prefs = context.getSharedPreferences("diagnostics", Context.MODE_PRIVATE)
        val exit = if (Build.VERSION.SDK_INT >= 30) {
            runCatching {
                context.getSystemService(ActivityManager::class.java)
                    .getHistoricalProcessExitReasons(context.packageName, 0, 1).firstOrNull()
            }.getOrNull()
        } else {
            null
        }
        val previous = when {
            Build.VERSION.SDK_INT < 30 -> "unknown (API < 30)"
            exit == null -> "none recorded"
            exit.timestamp == prefs.getLong(KEY_LAST_EXIT, 0) -> "not recorded"
            else -> {
                prefs.edit().putLong(KEY_LAST_EXIT, exit.timestamp).apply()
                "${exitReason(exit.reason)} at ${time.format(Date(exit.timestamp))}, status ${exit.status}, " +
                    "importance ${exit.importance}" + (exit.description?.let { ", \"$it\"" } ?: "")
            }
        }
        log("process start; previous process: $previous")
    }

    private fun exitReason(reason: Int) = when (reason) {
        ApplicationExitInfo.REASON_EXIT_SELF -> "exit_self"
        ApplicationExitInfo.REASON_SIGNALED -> "signaled"
        ApplicationExitInfo.REASON_LOW_MEMORY -> "low_memory"
        ApplicationExitInfo.REASON_CRASH -> "crash"
        ApplicationExitInfo.REASON_CRASH_NATIVE -> "crash_native"
        ApplicationExitInfo.REASON_ANR -> "anr"
        ApplicationExitInfo.REASON_INITIALIZATION_FAILURE -> "initialization_failure"
        ApplicationExitInfo.REASON_PERMISSION_CHANGE -> "permission_change"
        ApplicationExitInfo.REASON_EXCESSIVE_RESOURCE_USAGE -> "excessive_resource_usage"
        ApplicationExitInfo.REASON_USER_REQUESTED -> "user_requested"
        ApplicationExitInfo.REASON_USER_STOPPED -> "user_stopped"
        ApplicationExitInfo.REASON_DEPENDENCY_DIED -> "dependency_died"
        ApplicationExitInfo.REASON_OTHER -> "other"
        else -> "reason $reason"
    }

    private const val KEY_LAST_EXIT = "last_exit_ts"
}
