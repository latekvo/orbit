package dev.orbit.companion

import android.util.Log
import dev.orbit.protocol.SocketByteStream
import java.io.IOException
import java.net.InetSocketAddress
import java.net.ServerSocket

/** LAN transport (spec 1.1): accepts sessions on [PREFERRED_PORT], or any free port if taken. */
class TcpServer(private val orbit: Orbit) {
    private var server: ServerSocket? = null
    private var thread: Thread? = null

    /** Returns the bound port. */
    fun start(): Int {
        val ss = try {
            ServerSocket().apply { reuseAddress = true; bind(InetSocketAddress(PREFERRED_PORT)) }
        } catch (e: IOException) {
            Log.w(TAG, "port $PREFERRED_PORT unavailable, using a free port", e)
            ServerSocket(0)
        }
        server = ss
        thread = Thread({
            while (!ss.isClosed) {
                try {
                    val sock = ss.accept()
                    sock.tcpNoDelay = true
                    Log.i(TAG, "tcp connection from ${sock.remoteSocketAddress}")
                    orbit.startSession(SocketByteStream(sock), "lan")
                } catch (e: IOException) {
                    if (!ss.isClosed) Log.w(TAG, "accept failed", e)
                }
            }
        }, "orbit-tcp-accept").apply { isDaemon = true; start() }
        return ss.localPort
    }

    fun stop() {
        runCatching { server?.close() }
        server = null
        thread = null
    }

    companion object {
        const val PREFERRED_PORT = 47820
    }
}
