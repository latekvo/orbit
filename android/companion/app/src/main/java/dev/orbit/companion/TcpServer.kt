package dev.orbit.companion

import android.util.Log
import dev.orbit.protocol.SocketByteStream
import java.io.IOException
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket

/** LAN transport (spec 1.1): accepts sessions on [PREFERRED_PORT], or any free port if taken. */
class TcpServer(private val orbit: Orbit) {
    private var server: ServerSocket? = null
    val port: Int? get() = server?.localPort
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
            acceptLoop(ss, onError = { Log.w(TAG, "accept failed", it) }) { sock ->
                sock.tcpNoDelay = true
                Log.i(TAG, "tcp connection from ${sock.remoteSocketAddress}")
                orbit.startSession(SocketByteStream(sock), "lan", sock.inetAddress.hostAddress)
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

internal const val ACCEPT_BACKOFF_MIN_MS = 50L
internal const val ACCEPT_BACKOFF_MAX_MS = 2_000L

/**
 * Accepts connections until [server] closes. While accept keeps failing (e.g. the process is out
 * of file descriptors) it backs off exponentially instead of spinning; a success resets the delay.
 */
internal fun acceptLoop(
    server: ServerSocket,
    onError: (IOException) -> Unit,
    sleep: (Long) -> Unit = Thread::sleep,
    onAccept: (Socket) -> Unit,
) {
    var backoffMs = 0L
    while (!server.isClosed) {
        val sock = try {
            server.accept()
        } catch (e: IOException) {
            if (server.isClosed) return
            onError(e)
            backoffMs = (backoffMs * 2).coerceIn(ACCEPT_BACKOFF_MIN_MS, ACCEPT_BACKOFF_MAX_MS)
            sleep(backoffMs)
            continue
        }
        backoffMs = 0
        try {
            onAccept(sock)
        } catch (e: IOException) {
            onError(e)
            runCatching { sock.close() }
        }
    }
}
