package dev.orbit.sample

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Typeface
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.View
import android.view.WindowInsets
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.Switch
import android.widget.TextView

/**
 * Toggles per producer, in-app controls and a live state readout.
 * Headless start: `am start -n dev.orbit.sample/.MainActivity --es start media,download,timer,reply`
 * (also `--es stop <list>`; `all` selects every producer).
 */
class MainActivity : Activity() {
    private val handler = Handler(Looper.getMainLooper())
    private val switches = LinkedHashMap<String, Switch>()
    private lateinit var stateView: TextView
    private lateinit var logView: TextView
    private var syncing = false

    private val refresher = object : Runnable {
        override fun run() {
            refresh()
            handler.postDelayed(this, 500)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(buildUi())
        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
        }
        handleIntent(intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        handleIntent(intent)
    }

    private fun handleIntent(intent: Intent) {
        Producers.parseList(intent.getStringExtra("stop")).forEach { Producers.dispatch("$it.stop", source = "intent") }
        Producers.parseList(intent.getStringExtra("start")).forEach { Producers.dispatch("$it.start", source = "intent") }
        // Consume so a recreated activity does not replay the command.
        intent.removeExtra("start")
        intent.removeExtra("stop")
    }

    override fun onResume() {
        super.onResume()
        handler.post(refresher)
    }

    override fun onPause() {
        super.onPause()
        handler.removeCallbacks(refresher)
    }

    private fun ui(action: String, value: Long? = null, text: String? = null) {
        Producers.dispatch(action, value, text, source = "ui")
        refresh()
    }

    private fun buildUi(): View {
        val pad = (16 * resources.displayMetrics.density).toInt()
        val col = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, pad, pad, pad)
        }
        col.addView(TextView(this).apply {
            text = "Each switch starts or stops a producer. Actions from the shade, a mirror or adb update the state below."
        })
        fun row(vararg buttons: Pair<String, () -> Unit>) = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            buttons.forEach { (label, onClick) ->
                addView(Button(this@MainActivity).apply {
                    text = label
                    isAllCaps = false
                    setOnClickListener { onClick() }
                }, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
            }
        }
        fun section(name: String, label: String, controls: View) {
            val sw = Switch(this).apply {
                text = label
                textSize = 18f
                setPadding(0, pad, 0, 0)
                setOnCheckedChangeListener { _, checked ->
                    if (!syncing) ui(if (checked) "$name.start" else "$name.stop")
                }
            }
            switches[name] = sw
            col.addView(sw)
            col.addView(controls)
        }
        section(
            "media", "Media player",
            row("Prev" to { ui("media.previous") }, "Play" to { ui("media.play") },
                "Pause" to { ui("media.pause") }, "Next" to { ui("media.next") }),
        )
        section(
            "download", "Download",
            row("Pause" to { ui("download.pause") }, "Resume" to { ui("download.resume") },
                "Cancel" to { ui("download.cancel") }),
        )
        section(
            "timer", "Countdown timer",
            row("Pause" to { ui("timer.pause") }, "Resume" to { ui("timer.resume") },
                "+1 min" to { ui("timer.add_minute") }),
        )
        val input = EditText(this).apply { hint = "Reply text" }
        section(
            "reply", "Reply conversation",
            LinearLayout(this).apply {
                orientation = LinearLayout.HORIZONTAL
                addView(input, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
                addView(Button(this@MainActivity).apply {
                    text = "Send"
                    setOnClickListener { ui("reply.send", text = input.text.toString()); input.text.clear() }
                })
            },
        )
        col.addView(TextView(this).apply { text = "State"; textSize = 18f; setPadding(0, pad, 0, 0) })
        stateView = TextView(this).apply { typeface = Typeface.MONOSPACE; setTextIsSelectable(true) }
        col.addView(stateView)
        col.addView(TextView(this).apply { text = "Recent actions"; textSize = 18f; setPadding(0, pad, 0, 0) })
        logView = TextView(this).apply { typeface = Typeface.MONOSPACE; textSize = 11f }
        col.addView(logView)

        return ScrollView(this).apply {
            addView(col)
            // targetSdk 35 forces edge-to-edge on API 35+ devices only.
            if (Build.VERSION.SDK_INT >= 35) {
                setOnApplyWindowInsetsListener { v, insets ->
                    val bars = insets.getInsets(WindowInsets.Type.systemBars() or WindowInsets.Type.ime())
                    v.setPadding(bars.left, bars.top, bars.right, bars.bottom)
                    insets
                }
            }
        }
    }

    private fun refresh() {
        syncing = true
        switches.forEach { (name, sw) -> sw.isChecked = Producers.byName(name)?.active == true }
        syncing = false
        stateView.text = Producers.all.joinToString("\n") { it.describe() }
        logView.text = Producers.recent.joinToString("\n\n")
    }
}
