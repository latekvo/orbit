package dev.orbit.companion

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.IOException
import java.net.ServerSocket
import java.net.Socket
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

class TransportLimitsTest {
    @Test
    fun unauthenticatedSessionsAreCappedPerAddressAndInTotal() {
        val gate = UnauthenticatedSessions(maxTotal = 3, maxPerAddress = 2)
        assertTrue(gate.admit(1, "10.0.0.1"))
        assertTrue(gate.admit(2, "10.0.0.1"))
        assertFalse(gate.admit(3, "10.0.0.1"))
        assertTrue(gate.admit(4, "10.0.0.2"))
        assertFalse(gate.admit(5, "10.0.0.3"))
        gate.release(1)
        gate.release(1) // idempotent: must not free a second slot
        assertTrue(gate.admit(6, "10.0.0.1"))
        assertFalse(gate.admit(7, "10.0.0.3"))
        gate.release(99) // unknown id
        assertFalse(gate.admit(8, "10.0.0.3"))
        gate.release(4)
        assertTrue(gate.admit(9, "10.0.0.3"))
    }

    /** accept() fails [failures] times, succeeds once, fails twice more, then the server closes. */
    private class FlakyServer(private var failures: Int) : ServerSocket() {
        private var after = 2
        override fun accept(): Socket {
            if (failures > 0) { failures--; throw IOException("EMFILE") }
            if (failures == 0) { failures = -1; return Socket() }
            if (after-- > 0) throw IOException("EMFILE")
            close()
            throw IOException("socket closed")
        }
    }

    @Test
    fun acceptLoopBacksOffWhileAcceptFailsAndResetsOnSuccess() {
        val sleeps = mutableListOf<Long>()
        val errors = mutableListOf<IOException>()
        var accepted = 0
        acceptLoop(FlakyServer(8), onError = { errors += it }, sleep = { sleeps += it }) { accepted++ }
        assertEquals(listOf(50L, 100, 200, 400, 800, 1600, 2000, 2000, 50, 100), sleeps)
        assertEquals(1, accepted)
        assertEquals(10, errors.size) // the failure caused by closing is not reported
    }

    @Test
    fun acceptLoopKeepsGoingWhenSetupOfOneSocketFails() {
        val server = object : ServerSocket() {
            var n = 0
            override fun accept(): Socket {
                if (n++ < 2) return Socket()
                close()
                throw IOException("socket closed")
            }
        }
        val errors = mutableListOf<IOException>()
        val sockets = mutableListOf<Socket>()
        acceptLoop(server, onError = { errors += it }, sleep = { error("no backoff expected") }) {
            sockets += it
            throw IOException("setsockopt failed")
        }
        assertEquals(2, sockets.size)
        assertEquals(2, errors.size)
        assertTrue(sockets.all { it.isClosed })
    }

    @Test
    fun sessionThreadErrorsGoToTheHandlerNotTheProcessDefault() {
        val caught = CopyOnWriteArrayList<Throwable>()
        val fatal = CopyOnWriteArrayList<Throwable>()
        val previous = Thread.getDefaultUncaughtExceptionHandler()
        Thread.setDefaultUncaughtExceptionHandler { _, e -> fatal += e }
        try {
            val pool = Executors.newCachedThreadPool(daemonThreads("test-session") { caught += it })
            pool.execute { throw OutOfMemoryError("boom") }
            pool.shutdown()
            assertTrue(pool.awaitTermination(5, TimeUnit.SECONDS))
            Thread.sleep(100)
            assertEquals(listOf("boom"), caught.map { it.message })
            assertTrue(fatal.isEmpty())
        } finally {
            Thread.setDefaultUncaughtExceptionHandler(previous)
        }
    }
}
