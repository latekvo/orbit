package dev.orbit.companion

import android.content.Context
import android.os.Build
import android.provider.Settings
import android.util.Log
import dev.orbit.protocol.ActivityHub
import dev.orbit.protocol.ActivityPublisher
import dev.orbit.protocol.ByteStream
import dev.orbit.protocol.KeyPair
import dev.orbit.protocol.Peer
import dev.orbit.protocol.ResponderSession
import dev.orbit.protocol.SessionHost
import dev.orbit.protocol.deviceId
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.update
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledThreadPoolExecutor

const val TAG = "Orbit"

/** Executes `action` messages against the activity sources (implemented by the listener service). */
fun interface ActionExecutor {
    fun perform(activity: JsonObject, actionId: String, value: JsonElement?, done: (Boolean, String?) -> Unit)
}

data class SessionView(
    val id: Long,
    val transport: String,
    val peerId: String?,
    val peerName: String?,
    val state: ResponderSession.State,
    val sas: String?,
    val localDecision: Boolean?,
    val remoteDecision: Boolean?,
)

data class TransportStatus(
    val serviceRunning: Boolean = false,
    val tcpPort: Int? = null,
    val nsd: String = "stopped",
    val ble: String = "stopped",
)

/** Process-wide state shared by the foreground service, the listener service and the UI. */
class Orbit private constructor(private val context: Context) : SessionHost {
    val identity: KeyPair = Identity.loadOrCreate(context)
    val deviceId: String = deviceId(identity.public)
    val peers = PeerStore(context)
    val hub = ActivityHub()
    val scheduler = ScheduledThreadPoolExecutor(2).apply { removeOnCancelPolicy = true }
    val publisher = ActivityPublisher(hub, scheduler)
    private val sessionThreads = Executors.newCachedThreadPool { r -> Thread(r, "orbit-session").apply { isDaemon = true } }
    private val sessions = ConcurrentHashMap<Long, ResponderSession>()

    @Volatile var actionExecutor: ActionExecutor? = null

    private val _sessions = MutableStateFlow<List<SessionView>>(emptyList())
    val sessionViews: StateFlow<List<SessionView>> = _sessions
    private val _activities = MutableStateFlow<List<JsonObject>>(emptyList())
    val activities: StateFlow<List<JsonObject>> = _activities
    val transports = MutableStateFlow(TransportStatus())
    val listenerConnected = MutableStateFlow(false)

    private val pairingNotifier = PairingNotifier(context)

    init {
        hub.observe { list -> _activities.value = list }
    }

    val deviceName: String
        get() = Settings.Global.getString(context.contentResolver, Settings.Global.DEVICE_NAME)?.takeIf { it.isNotBlank() }
            ?: Build.MODEL

    /** Runs a responder session over [stream] on a background thread. */
    fun startSession(stream: ByteStream, transport: String) {
        val session = ResponderSession(stream, transport, identity, deviceName, "android", hub, this, scheduler)
        sessions[session.id] = session
        sessionChanged(session)
        sessionThreads.execute {
            session.run()
            Log.i(TAG, "session ${session.id} ($transport) ended: ${session.closeReason}")
        }
    }

    fun session(id: Long): ResponderSession? = sessions[id]

    fun shutdownSessions(reason: String) = sessions.values.forEach { it.shutdown(reason) }

    /** Closes sessions using [transport] (e.g. when BLE goes away). */
    fun closeSessions(transport: String, reason: String) =
        sessions.values.filter { it.transport == transport }.forEach { it.close(reason) }

    fun forget(peerId: String) {
        peers.remove(peerId)
        sessions.values.filter { it.peer?.id == peerId }.forEach { it.unpairAndClose() }
        refreshSessions()
    }

    // --- SessionHost ---

    override fun isPinned(peer: Peer) = peers.isPinned(peer)

    override fun pin(peer: Peer) {
        peers.pin(peer)
        Log.i(TAG, "paired with ${peer.name} (${peer.id})")
    }

    override fun unpin(peerId: String) {
        peers.remove(peerId)
    }

    override fun sessionChanged(session: ResponderSession) {
        if (session.state == ResponderSession.State.CLOSED) sessions.remove(session.id)
        if (session.state == ResponderSession.State.PAIRING && session.localDecision == null) {
            pairingNotifier.show(session)
        } else {
            pairingNotifier.cancel(session.id)
        }
        refreshSessions()
    }

    override fun performAction(activity: JsonObject, actionId: String, value: JsonElement?, done: (Boolean, String?) -> Unit) {
        val exec = actionExecutor ?: return done(false, "notification access not granted")
        exec.perform(activity, actionId, value, done)
    }

    private fun refreshSessions() {
        _sessions.update {
            sessions.values.sortedBy { it.id }.map {
                SessionView(it.id, it.transport, it.peer?.id, it.peer?.name, it.state, it.sas, it.localDecision, it.remoteDecision)
            }
        }
    }

    companion object {
        @Volatile private var instance: Orbit? = null

        fun get(context: Context): Orbit = instance ?: synchronized(this) {
            instance ?: Orbit(context.applicationContext).also { instance = it }
        }
    }
}
