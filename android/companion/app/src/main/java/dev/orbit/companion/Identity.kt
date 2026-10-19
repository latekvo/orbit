package dev.orbit.companion

import android.content.Context
import dev.orbit.protocol.KeyPair
import java.io.File

/** Long-term P-256 identity, generated on first run and kept in app-private storage. */
object Identity {
    private const val FILE = "identity.key"

    fun loadOrCreate(context: Context): KeyPair {
        val file = File(context.noBackupFilesDir, FILE)
        if (file.exists()) {
            runCatching {
                val bytes = file.readBytes()
                return KeyPair.fromStored(bytes.copyOfRange(0, 32), bytes.copyOfRange(32, 97))
            }
        }
        val kp = KeyPair.generate()
        val tmp = File(file.parentFile, "$FILE.tmp")
        tmp.writeBytes(kp.scalar + kp.public)
        check(tmp.renameTo(file)) { "could not persist identity" }
        return kp
    }
}
