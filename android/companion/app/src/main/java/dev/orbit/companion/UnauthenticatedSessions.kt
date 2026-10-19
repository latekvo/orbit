package dev.orbit.companion

/**
 * Admission limits for sessions that are not yet paired (handshake, unpaired, pairing): anyone on
 * the network can open those, so they are capped in total and per remote address. Thread-safe.
 */
class UnauthenticatedSessions(private val maxTotal: Int, private val maxPerAddress: Int) {
    private val held = HashMap<Long, String>()
    private val perAddress = HashMap<String, Int>()

    /** Counts session [id] from [address] against the limits; false if either is reached. */
    @Synchronized
    fun admit(id: Long, address: String): Boolean {
        val n = perAddress[address] ?: 0
        if (held.size >= maxTotal || n >= maxPerAddress) return false
        held[id] = address
        perAddress[address] = n + 1
        return true
    }

    /** Stops counting session [id] (it paired or ended). Idempotent. */
    @Synchronized
    fun release(id: Long) {
        val address = held.remove(id) ?: return
        val n = perAddress.getValue(address) - 1
        if (n == 0) perAddress.remove(address) else perAddress[address] = n
    }
}
