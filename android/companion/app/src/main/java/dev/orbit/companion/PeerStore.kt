package dev.orbit.companion

import android.content.Context
import dev.orbit.protocol.Peer
import dev.orbit.protocol.b64d
import dev.orbit.protocol.b64e
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put

data class PinnedPeer(val id: String, val name: String, val platform: String, val static: ByteArray, val pairedMs: Long)

/** Pinned peers (spec 3.4): deviceId -> static public key + name. */
class PeerStore(context: Context) {
    private val prefs = context.getSharedPreferences("peers", Context.MODE_PRIVATE)
    private val _peers = MutableStateFlow(load())
    val peers: StateFlow<List<PinnedPeer>> = _peers

    private fun load(): List<PinnedPeer> = prefs.all.mapNotNull { (id, v) ->
        runCatching {
            val o = Json.parseToJsonElement(v as String).jsonObject
            PinnedPeer(
                id, o["name"]!!.jsonPrimitive.content, o["platform"]!!.jsonPrimitive.content,
                b64d(o["static"]!!.jsonPrimitive.content), o["pairedMs"]!!.jsonPrimitive.content.toLong(),
            )
        }.getOrNull()
    }.sortedBy { it.pairedMs }

    @Synchronized
    fun isPinned(peer: Peer): Boolean = _peers.value.any { it.id == peer.id && it.static.contentEquals(peer.static) }

    @Synchronized
    fun pin(peer: Peer) {
        val o: JsonObject = buildJsonObject {
            put("name", peer.name)
            put("platform", peer.platform)
            put("static", b64e(peer.static))
            put("pairedMs", System.currentTimeMillis())
        }
        prefs.edit().putString(peer.id, o.toString()).commit()
        _peers.value = load()
    }

    @Synchronized
    fun remove(id: String) {
        prefs.edit().remove(id).commit()
        _peers.value = load()
    }
}
