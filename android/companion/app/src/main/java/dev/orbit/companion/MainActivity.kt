package dev.orbit.companion

import android.Manifest
import android.content.ClipData
import android.content.ClipboardManager
import android.content.ComponentName
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.lifecycle.LifecycleEventObserver
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import dev.orbit.protocol.ResponderSession
import dev.orbit.protocol.POSITION_AT
import dev.orbit.protocol.bool
import dev.orbit.protocol.long
import dev.orbit.protocol.str
import kotlinx.coroutines.delay
import kotlinx.serialization.json.JsonPrimitive
import android.graphics.BitmapFactory
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.Image
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.asPaddingValues
import androidx.compose.foundation.layout.calculateEndPadding
import androidx.compose.foundation.layout.calculateStartPadding
import androidx.compose.foundation.layout.safeDrawing
import androidx.compose.foundation.layout.size
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.dynamicDarkColorScheme
import androidx.compose.material3.dynamicLightColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.platform.LocalLayoutDirection
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject

private const val DIAGNOSTIC_LINES = 200
private const val KEY_BLOCKED = "blocked_permissions"

/** m:ss, or h:mm:ss from an hour. */
private fun clock(ms: Long): String {
    val t = ms / 1000
    return if (t >= 3600) "%d:%02d:%02d".format(t / 3600, t / 60 % 60, t % 60) else "%d:%02d".format(t / 60, t % 60)
}

class MainActivity : ComponentActivity() {
    private val permissionLauncher = registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { result ->
        // Android stops showing the dialog after repeated denials; only Settings can grant them then.
        val blocked = result.filter { !it.value && !shouldShowRequestPermissionRationale(it.key) }.keys
        uiPrefs.edit().putStringSet(KEY_BLOCKED, blockedPermissions() + blocked).apply()
        permissionTick.intValue++
        OrbitService.start(this, OrbitService.ACTION_ENSURE_BLE, source = "permission result")
    }
    private val permissionTick = mutableIntStateOf(0)
    private val uiPrefs by lazy { getSharedPreferences("ui", MODE_PRIVATE) }

    private fun blockedPermissions(): Set<String> = uiPrefs.getStringSet(KEY_BLOCKED, null).orEmpty()

    override fun onCreate(savedInstanceState: Bundle?) {
        enableEdgeToEdge()
        super.onCreate(savedInstanceState)
        val orbit = Orbit.get(this)
        OrbitService.start(this, source = "app opened")
        setContent {
            val dark = isSystemInDarkTheme()
            val colors = when {
                Build.VERSION.SDK_INT >= 31 && dark -> dynamicDarkColorScheme(this)
                Build.VERSION.SDK_INT >= 31 -> dynamicLightColorScheme(this)
                dark -> darkColorScheme()
                else -> lightColorScheme()
            }
            MaterialTheme(colorScheme = colors) {
                Surface(Modifier.fillMaxSize()) {
                    Screen(orbit)
                }
            }
        }
    }

    override fun onResume() {
        super.onResume()
        Orbit.get(this).uiVisible = true
    }

    override fun onPause() {
        Orbit.get(this).uiVisible = false
        super.onPause()
    }

    private fun missingPermissions(): List<String> {
        val wanted = buildList {
            if (Build.VERSION.SDK_INT >= 33) add(Manifest.permission.POST_NOTIFICATIONS)
            if (Build.VERSION.SDK_INT >= 31) {
                add(Manifest.permission.BLUETOOTH_CONNECT)
                add(Manifest.permission.BLUETOOTH_ADVERTISE)
            }
        }
        return wanted.filter { checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED }
    }

    private fun listenerGranted(): Boolean {
        val flat = Settings.Secure.getString(contentResolver, "enabled_notification_listeners").orEmpty()
        val me = ComponentName(this, OrbitListenerService::class.java)
        return flat.split(':').any { ComponentName.unflattenFromString(it) == me }
    }

    @Composable
    private fun Screen(orbit: Orbit) {
        val transports by orbit.transports.collectAsStateWithLifecycle()
        val sessions by orbit.sessionViews.collectAsStateWithLifecycle()
        val peers by orbit.peers.peers.collectAsStateWithLifecycle()
        val activities by orbit.activities.collectAsStateWithLifecycle()
        val connected by orbit.listenerConnected.collectAsStateWithLifecycle()
        val name by orbit.deviceNameFlow.collectAsStateWithLifecycle()
        var nameDraft by remember { mutableStateOf(orbit.customName ?: "") }
        var resumeTick by remember { mutableStateOf(0) }
        val owner = LocalLifecycleOwner.current
        DisposableEffect(owner) {
            val obs = LifecycleEventObserver { _, e -> if (e == Lifecycle.Event.ON_RESUME) resumeTick++ }
            owner.lifecycle.addObserver(obs)
            onDispose { owner.lifecycle.removeObserver(obs) }
        }
        val granted = remember(resumeTick, connected) { listenerGranted() }
        val missing = remember(resumeTick, permissionTick.intValue) { missingPermissions() }
        val blocked = missing.isNotEmpty() && missing.all { it in blockedPermissions() && !shouldShowRequestPermissionRationale(it) }
        var now by remember { mutableLongStateOf(System.currentTimeMillis()) }
        LaunchedEffect(Unit) {
            while (true) {
                delay(1000)
                now = System.currentTimeMillis()
            }
        }
        val overlay = remember(resumeTick) { Settings.canDrawOverlays(this) }
        val unrestricted = remember(resumeTick) { Background.ignoringBatteryOptimizations(this) }
        val oem = remember { Background.oemBrand(Build.MANUFACTURER) }
        var diagnostics by remember { mutableStateOf("") }
        var showDiagnostics by remember { mutableStateOf(false) }
        LaunchedEffect(resumeTick, showDiagnostics) { if (showDiagnostics) diagnostics = EventLog.read() }

        var forgetting by remember { mutableStateOf<PinnedPeer?>(null) }
        forgetting?.let { p ->
            AlertDialog(
                onDismissRequest = { forgetting = null },
                title = { Text("Forget ${p.name}?") },
                text = { Text("It will need to pair again to see your activities.") },
                confirmButton = { Button(onClick = { orbit.forget(p.id); forgetting = null }) { Text("Forget") } },
                dismissButton = { TextButton(onClick = { forgetting = null }) { Text("Cancel") } },
            )
        }

        sessions.firstOrNull { it.state == ResponderSession.State.PAIRING && it.localDecision == null }?.let { p ->
            AlertDialog(
                onDismissRequest = {},
                title = { Text("Pair with ${p.peerName}?") },
                text = {
                    Column {
                        Text("Check that this code matches the one on the computer:")
                        Text(
                            p.sas.orEmpty().chunked(3).joinToString(" "),
                            fontSize = 40.sp, fontWeight = FontWeight.Bold, fontFamily = FontFamily.Monospace,
                            modifier = Modifier.padding(top = 16.dp),
                        )
                    }
                },
                confirmButton = { Button(onClick = { orbit.session(p.id)?.decidePairing(true) }) { Text("Accept") } },
                dismissButton = { TextButton(onClick = { orbit.session(p.id)?.decidePairing(false) }) { Text("Reject") } },
            )
        }

        val bars = WindowInsets.safeDrawing.asPaddingValues()
        val dir = LocalLayoutDirection.current
        LazyColumn(
            Modifier.fillMaxSize(),
            contentPadding = PaddingValues(
                start = bars.calculateStartPadding(dir) + 16.dp, end = bars.calculateEndPadding(dir) + 16.dp,
                top = bars.calculateTopPadding() + 16.dp, bottom = bars.calculateBottomPadding() + 16.dp,
            ),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            item {
                Text("Orbit", style = MaterialTheme.typography.headlineMedium)
                Text("$name · ${orbit.deviceId}", style = MaterialTheme.typography.bodySmall)
            }
            item {
                Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    OutlinedTextField(
                        value = nameDraft,
                        onValueChange = { nameDraft = it },
                        label = { Text("Device name") },
                        placeholder = { Text(DeviceNames.system(this@MainActivity)) },
                        singleLine = true,
                        modifier = Modifier.weight(1f),
                    )
                    Button(onClick = { orbit.setCustomName(nameDraft) }) { Text("Save") }
                }
            }
            item {
                Card(Modifier.fillMaxWidth()) {
                    Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                        Text("Status", fontWeight = FontWeight.Bold)
                        Text("Service: " + if (transports.serviceRunning) "running" else "stopped")
                        Text("LAN: " + (transports.tcpPort?.let { "TCP port $it" } ?: "-") + ", mDNS ${transports.nsd}")
                        Text("Bluetooth LE: ${transports.ble}")
                        Text("Notification access: " + if (granted) (if (connected) "granted" else "granted (connecting)") else "not granted")
                        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            if (transports.serviceRunning) {
                                OutlinedButton(onClick = { OrbitService.stop(this@MainActivity) }) { Text("Stop") }
                            } else {
                                Button(onClick = { OrbitService.start(this@MainActivity, source = "Start button", byUser = true) }) { Text("Start") }
                            }
                            if (!granted) {
                                Button(onClick = { startActivity(Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS)) }) {
                                    Text("Grant access")
                                }
                            }
                        }
                        Text("Open apps from computer: " + if (overlay) "allowed" else "not allowed")
                        if (!overlay) {
                            OutlinedButton(onClick = {
                                startActivity(Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, Uri.parse("package:$packageName")))
                            }) { Text("Allow opening apps") }
                        }
                        if (blocked) {
                            Text(
                                "Notifications or Bluetooth are turned off for Orbit. Without them pairing prompts and " +
                                    "Bluetooth connections don't work. Turn them on under Permissions in Settings.",
                                style = MaterialTheme.typography.bodySmall,
                            )
                            Button(onClick = {
                                startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:$packageName")))
                            }) { Text("Open settings") }
                        } else if (missing.isNotEmpty()) {
                            Button(onClick = { permissionLauncher.launch(missing.toTypedArray()) }) { Text("Allow notifications & Bluetooth") }
                        }
                    }
                }
            }
            if (!unrestricted || oem != null) {
                item {
                    Card(Modifier.fillMaxWidth()) {
                        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                            Text("Keep Orbit running", fontWeight = FontWeight.Bold)
                            Text(
                                "To save battery the phone may stop Orbit in the background, which disconnects your computers.",
                                style = MaterialTheme.typography.bodySmall,
                            )
                            if (!unrestricted) {
                                Button(onClick = { launch(Background.batteryExemptionIntent(this@MainActivity)) }) {
                                    Text("Allow running in background")
                                }
                            }
                            if (oem != null) {
                                Text(
                                    "$oem phones also stop apps on their own. Allow Orbit to auto-start and run in the background.",
                                    style = MaterialTheme.typography.bodySmall,
                                )
                                OutlinedButton(onClick = { launch(Background.oemSettingsIntent(this@MainActivity, oem)) }) {
                                    Text("Open $oem settings")
                                }
                            }
                        }
                    }
                }
            }
            item { Text("Connected sessions", fontWeight = FontWeight.Bold) }
            if (sessions.isEmpty()) item { Text("None", style = MaterialTheme.typography.bodySmall) }
            items(sessions, key = { "s${it.id}" }) { s ->
                Text("${s.peerName ?: "(handshake)"} · ${s.transport} · ${s.state.name.lowercase()}")
            }
            item { Text("Paired computers", fontWeight = FontWeight.Bold) }
            if (peers.isEmpty()) item { Text("None", style = MaterialTheme.typography.bodySmall) }
            items(peers, key = { "p${it.id}" }) { p ->
                Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                    Column(Modifier.weight(1f)) {
                        Text(p.name)
                        Text("${p.platform} · ${p.id.take(8)}", style = MaterialTheme.typography.bodySmall)
                    }
                    OutlinedButton(
                        onClick = { forgetting = p },
                        modifier = Modifier.semantics { contentDescription = "Forget ${p.name}" },
                    ) { Text("Forget") }
                }
            }
            item { Text("Exported activities (${activities.size})", fontWeight = FontWeight.Bold) }
            items(activities, key = { "a" + it.str("id") }) { a -> ActivityRow(orbit, a, now) }
            item {
                Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text("Diagnostics", fontWeight = FontWeight.Bold, modifier = Modifier.weight(1f))
                    TextButton(onClick = { showDiagnostics = !showDiagnostics }) { Text(if (showDiagnostics) "Hide" else "Show") }
                }
            }
            if (showDiagnostics) {
                item {
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        OutlinedButton(onClick = { diagnostics = EventLog.read() }) { Text("Refresh") }
                        OutlinedButton(onClick = {
                            getSystemService(ClipboardManager::class.java).setPrimaryClip(ClipData.newPlainText("Orbit events", EventLog.read()))
                        }) { Text("Copy") }
                        OutlinedButton(onClick = {
                            val send = Intent(Intent.ACTION_SEND).setType("text/plain").putExtra(Intent.EXTRA_TEXT, EventLog.read())
                            startActivity(Intent.createChooser(send, "Share Orbit events"))
                        }) { Text("Share") }
                    }
                }
                item {
                    Text(
                        diagnostics.lines().takeLast(DIAGNOSTIC_LINES).joinToString("\n").ifBlank { "No events" },
                        style = MaterialTheme.typography.bodySmall, fontFamily = FontFamily.Monospace,
                    )
                }
            }
        }
    }

    /** Starts a settings screen; OEM screens may refuse callers, so fall back to the app details. */
    private fun launch(intent: Intent) {
        try {
            startActivity(intent)
        } catch (e: RuntimeException) {
            EventLog.log("could not open ${intent.component ?: intent.action}: $e")
            startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:$packageName")))
        }
    }

    @Composable
    private fun ActivityRow(orbit: Orbit, a: JsonObject, now: Long) {
        val icon = remember(a.str("iconKey")) {
            a.str("iconKey")?.let { orbit.hub.art(it) }?.let { BitmapFactory.decodeByteArray(it.data, 0, it.data.size)?.asImageBitmap() }
        }
        Card(Modifier.fillMaxWidth()) {
            Row(Modifier.padding(12.dp), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                if (icon != null) Image(icon, contentDescription = null, modifier = Modifier.size(32.dp))
                Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(2.dp)) {
                    Text((a["app"] as? JsonObject)?.str("name").orEmpty(), style = MaterialTheme.typography.labelSmall)
                    Text(a.str("title").orEmpty(), fontWeight = FontWeight.Bold)
                    listOfNotNull(a.str("subtitle"), a.str("text")).forEach { Text(it, style = MaterialTheme.typography.bodySmall) }
                    (a["media"] as? JsonObject)?.let { MediaLine(it, now) }
                    (a["progress"] as? JsonObject)?.let { p ->
                        val max = p.long("max") ?: 0
                        if (p.bool("indeterminate") == true || max <= 0) {
                            LinearProgressIndicator(Modifier.fillMaxWidth())
                        } else {
                            LinearProgressIndicator(progress = { (p.long("value") ?: 0).toFloat() / max }, modifier = Modifier.fillMaxWidth())
                        }
                    }
                    (a["chrono"] as? JsonObject)?.let { c ->
                        val base = c.long("baseMs") ?: 0
                        val ms = when {
                            c.bool("running") == false -> c.long("valueMs") ?: 0
                            c.bool("countDown") == true -> base - now
                            else -> now - base
                        }
                        Text(clock(ms.coerceAtLeast(0)), style = MaterialTheme.typography.bodyMedium, fontFamily = FontFamily.Monospace)
                    }
                    val buttons = (a["actions"] as? JsonArray).orEmpty().mapNotNull { it as? JsonObject }
                        .filter { it.bool("overflow") != true && it.str("id") != "open" }.mapNotNull { it.str("label") }
                    if (buttons.isNotEmpty()) Text(buttons.joinToString(" · "), style = MaterialTheme.typography.bodySmall)
                }
            }
        }
    }

    @Composable
    private fun MediaLine(m: JsonObject, now: Long) {
        val state = m.str("state").orEmpty()
        val at = m.long(POSITION_AT)
        val rate = (m["rate"] as? JsonPrimitive)?.content?.toDoubleOrNull() ?: 1.0
        val duration = m.long("durationMs")
        val position = m.long("positionMs")?.let { pos ->
            val elapsed = if (state == "playing" && at != null) ((now - at) * rate).toLong() else 0
            (pos + elapsed).let { if (duration != null) it.coerceAtMost(duration) else it }
        }
        val time = listOfNotNull(position?.let(::clock), duration?.let(::clock)).joinToString(" / ")
        Text(listOf(state.replaceFirstChar { it.uppercase() }, time).filter { it.isNotEmpty() }.joinToString(" · "),
            style = MaterialTheme.typography.bodySmall)
        if (position != null && duration != null && duration > 0) {
            LinearProgressIndicator(progress = { position.toFloat() / duration }, modifier = Modifier.fillMaxWidth())
        }
    }
}
