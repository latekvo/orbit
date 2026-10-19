package dev.orbit.protocol

import java.io.BufferedOutputStream
import java.io.InputStream
import java.io.OutputStream
import java.net.Socket

class SocketByteStream(val socket: Socket) : ByteStream {
    override val input: InputStream = socket.getInputStream()
    override val output: OutputStream = BufferedOutputStream(socket.getOutputStream())

    override fun close() = socket.close()
}
