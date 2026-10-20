package dev.orbit.companion

/**
 * Admission limits for sessions that are not yet paired (handshake, unpaired, pairing): anyone on
 * the network can open those, so they are capped in total and per remote address. At a limit the
 * oldest session counted against it gives way instead of the new one being refused: pairing holds
 * a slot for up to 120 s, and refusing would lock a paired peer out for that long. Thread-safe.
 */
class UnauthenticatedSessions(private val maxTotal: Int, private val maxPerAddress: Int) {
    private val held = LinkedHashMap<Long, String>()
    private val perAddress = HashMap<String, Int>()

    /** Counts session [id] from [address]; returns the session it displaces, which the caller closes. */
    @Synchronized
    fun admit(id: Long, address: String): Long? {
        val evicted = when {
            (perAddress[address] ?: 0) >= maxPerAddress -> held.entries.first { it.value == address }.key
            held.size >= maxTotal -> held.keys.first()
            else -> null
        }
        evicted?.let(::release)
        held[id] = address
        perAddress[address] = (perAddress[address] ?: 0) + 1
        return evicted
    }

    /** Stops counting session [id] (it paired or ended). Idempotent. */
    @Synchronized
    fun release(id: Long) {
        val address = held.remove(id) ?: return
        val n = perAddress.getValue(address) - 1
        if (n == 0) perAddress.remove(address) else perAddress[address] = n
    }
}
