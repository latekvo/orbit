package dev.orbit.companion

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Test
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream

class BleLinksTest {
    private class FakeLink(override val kind: LinkKind) : BleSessionLink {
        val closes = mutableListOf<Pair<String, Boolean>>()
        override fun close(reason: String, dropConnection: Boolean) {
            closes += reason to dropConnection
        }
    }

    @Test
    fun aNewerSessionOfTheSameCentralReplacesTheOlderWithoutDroppingTheConnection() {
        val links = BleLinks<FakeLink>()
        val gatt = FakeLink(LinkKind.GATT)
        val channel = FakeLink(LinkKind.CHANNEL)
        val gatt2 = FakeLink(LinkKind.GATT)
        val other = FakeLink(LinkKind.GATT)
        links.open("A", gatt)
        links.open("B", other)
        links.open("A", channel)
        assertEquals(listOf("replaced by l2cap channel" to false), gatt.closes)
        links.open("A", gatt2)
        assertEquals(listOf("replaced by gatt" to false), channel.closes)
        assertSame(gatt2, links["A"])
        assertEquals(emptyList<Pair<String, Boolean>>(), other.closes)
    }

    @Test
    fun disablingNotificationsEndsOnlyAGattSession() {
        val links = BleLinks<FakeLink>()
        val channel = FakeLink(LinkKind.CHANNEL)
        links.open("A", channel)
        links.unsubscribed("A")
        assertEquals(emptyList<Pair<String, Boolean>>(), channel.closes)
        assertSame(channel, links["A"])

        val gatt = FakeLink(LinkKind.GATT)
        links.open("B", gatt)
        links.unsubscribed("B")
        assertEquals(listOf("notifications disabled" to true), gatt.closes)
        assertNull(links["B"])
    }

    @Test
    fun disconnectAndStopCloseEitherKind() {
        val links = BleLinks<FakeLink>()
        val channel = FakeLink(LinkKind.CHANNEL)
        val gatt = FakeLink(LinkKind.GATT)
        links.open("A", channel)
        links.open("B", gatt)
        links.disconnected("A")
        assertEquals(listOf("disconnected" to false), channel.closes)
        assertNull(links["A"])
        links.closeAll("stopped")
        assertEquals(listOf("stopped" to true), gatt.closes)
        assertNull(links["B"])
    }

    @Test
    fun removeTakesOnlyTheGivenLink() {
        val links = BleLinks<FakeLink>()
        val old = FakeLink(LinkKind.GATT)
        val new = FakeLink(LinkKind.CHANNEL)
        links.open("A", old)
        links.open("A", new)
        assertEquals(false, links.remove("A", old)) // a replaced link closing must not evict its successor
        assertSame(new, links["A"])
    }

    @Test
    fun meterCountsBytesBothWaysOverTheLinksDuration() {
        var now = 1_000L
        val meter = LinkMeter { now }
        val input = meter.input(ByteArrayInputStream(ByteArray(300)))
        input.read(ByteArray(200), 0, 200)
        input.read()
        val sink = ByteArrayOutputStream()
        val output = meter.output(sink)
        output.write(ByteArray(5_000), 0, 5_000)
        output.write(7)
        now += 2_000
        assertEquals(5_001, sink.size())
        assertEquals("sent 5001 B, received 201 B in 2.0 s (2500 B/s out)", meter.summary())
    }

    @Test
    fun notificationsCarryUpToMtuMinus3BytesCappedAt512() {
        assertEquals(20, notificationChunk(null))
        assertEquals(20, notificationChunk(23))
        assertEquals(244, notificationChunk(247))
        assertEquals(509, notificationChunk(512))
        assertEquals(512, notificationChunk(517))
    }

    @Test
    fun psmIsU16LittleEndian() {
        assertArrayEquals(byteArrayOf(0x80.toByte(), 0x00), psmValue(0x0080))
        assertArrayEquals(byteArrayOf(0x34, 0x12), psmValue(0x1234))
        assertArrayEquals(byteArrayOf(0xff.toByte(), 0xff.toByte()), psmValue(0xffff))
    }

    @Test(expected = IllegalArgumentException::class)
    fun psmBeyondU16IsRejected() {
        psmValue(0x10000)
    }

    @Test
    fun psmReadsRespectTheOffsetAndFailWhileNotListening() {
        val value = psmValue(0x00c1)
        assertEquals(0, readResponse(value, 0).first)
        assertArrayEquals(byteArrayOf(0xc1.toByte(), 0x00), readResponse(value, 0).second)
        assertArrayEquals(byteArrayOf(0x00), readResponse(value, 1).second)
        assertEquals(0, readResponse(value, 2).first)
        assertArrayEquals(ByteArray(0), readResponse(value, 2).second)
        assertEquals(0x07 to null, readResponse(value, 3)) // ATT Invalid Offset
        assertEquals(0x07 to null, readResponse(value, -1))
        assertEquals(0x0E to null, readResponse(null, 0)) // ATT Unlikely Error: the central falls back to RX/TX
    }
}
