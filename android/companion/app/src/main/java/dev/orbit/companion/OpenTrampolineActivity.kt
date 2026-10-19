package dev.orbit.companion

import android.app.Activity
import android.app.PendingIntent
import android.os.Build
import android.os.Bundle
import android.util.Log

/** Invisible activity that sends an `open` PendingIntent from the foreground, then finishes. */
class OpenTrampolineActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val pi = if (Build.VERSION.SDK_INT >= 33) {
            intent.getParcelableExtra(EXTRA_INTENT, PendingIntent::class.java)
        } else {
            @Suppress("DEPRECATION")
            intent.getParcelableExtra(EXTRA_INTENT)
        }
        try {
            pi?.send(this, 0, null, null, null, null, allowBackgroundStart())
        } catch (e: PendingIntent.CanceledException) {
            Log.w(TAG, "open: intent cancelled", e)
        }
        finish()
        @Suppress("DEPRECATION")
        overridePendingTransition(0, 0)
    }

    companion object {
        const val EXTRA_INTENT = "intent"
    }
}
