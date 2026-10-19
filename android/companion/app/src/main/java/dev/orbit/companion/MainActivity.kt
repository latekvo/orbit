package dev.orbit.companion

import android.Manifest
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
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
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
import dev.orbit.protocol.ResponderSession
import dev.orbit.protocol.str
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject

class MainActivity : ComponentActivity() {
    private val permissionLauncher = registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) {
        OrbitService.start(this, OrbitService.ACTION_RESTART_BLE)
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val orbit = Orbit.get(this)
        OrbitService.start(this)
        setContent {
            MaterialTheme {
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
        var resumeTick by remember { mutableStateOf(0) }
        val owner = LocalLifecycleOwner.current
        DisposableEffect(owner) {
            val obs = LifecycleEventObserver { _, e -> if (e == Lifecycle.Event.ON_RESUME) resumeTick++ }
            owner.lifecycle.addObserver(obs)
            onDispose { owner.lifecycle.removeObserver(obs) }
        }
        val granted = remember(resumeTick, connected) { listenerGranted() }
        val missing = remember(resumeTick) { missingPermissions() }
        val overlay = remember(resumeTick) { Settings.canDrawOverlays(this) }

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

        LazyColumn(Modifier.fillMaxSize().padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            item {
                Text("Orbit", style = MaterialTheme.typography.headlineMedium)
                Text("${orbit.deviceName} · ${orbit.deviceId}", style = MaterialTheme.typography.bodySmall)
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
                                Button(onClick = { OrbitService.start(this@MainActivity) }) { Text("Start") }
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
                        if (missing.isNotEmpty()) {
                            Button(onClick = { permissionLauncher.launch(missing.toTypedArray()) }) { Text("Allow notifications & Bluetooth") }
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
                    OutlinedButton(onClick = { forgetting = p }) { Text("Forget") }
                }
            }
            item { Text("Exported activities (${activities.size})", fontWeight = FontWeight.Bold) }
            items(activities, key = { "a" + it.str("id") }) { a -> ActivityRow(a) }
        }
    }

    @Composable
    private fun ActivityRow(a: JsonObject) {
        Card(Modifier.fillMaxWidth()) {
            Column(Modifier.padding(12.dp)) {
                Text("${a.str("kind")} · ${(a["app"] as? JsonObject)?.str("name")}", style = MaterialTheme.typography.labelSmall)
                Text(a.str("title").orEmpty(), fontWeight = FontWeight.Bold)
                listOfNotNull(a.str("subtitle"), a.str("text")).forEach { Text(it, style = MaterialTheme.typography.bodySmall) }
                a["media"]?.let { Text("media: $it", style = MaterialTheme.typography.bodySmall) }
                a["progress"]?.let { Text("progress: $it", style = MaterialTheme.typography.bodySmall) }
                a["chrono"]?.let { Text("chrono: $it", style = MaterialTheme.typography.bodySmall) }
                val actions = (a["actions"] as? JsonArray)?.mapNotNull { (it as? JsonObject)?.str("label") }.orEmpty()
                if (actions.isNotEmpty()) Text("actions: " + actions.joinToString(", "), style = MaterialTheme.typography.bodySmall)
                Text(a.str("id").orEmpty(), style = MaterialTheme.typography.labelSmall, fontFamily = FontFamily.Monospace)
            }
        }
    }
}
