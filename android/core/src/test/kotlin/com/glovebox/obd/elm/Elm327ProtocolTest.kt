package com.glovebox.obd.elm

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertTrue

class Elm327ProtocolTest {

    // ---- DTC parsing: service 03 (SID 0x43) ----

    @Test fun parseDtcs03_single() {
        assertEquals(listOf("P0171"), Elm327Protocol.parseDtcs("43 01 71"))
    }

    @Test fun parseDtcs03_multiple() {
        assertEquals(listOf("P0133", "P0171"), Elm327Protocol.parseDtcs("43 01 33 01 71"))
    }

    @Test fun parseDtcs03_empty() {
        assertEquals(emptyList(), Elm327Protocol.parseDtcs("43 00"))
    }

    @Test fun parseDtcs03_uCode() {
        assertEquals(listOf("U0100"), Elm327Protocol.parseDtcs("43 C1 00"))
    }

    // ---- DTC parsing: service 07 / 0A need their own SIDs ----

    @Test fun parseDtcs07_pendingSid() {
        assertEquals(listOf("P0171"), Elm327Protocol.parseDtcs("47 01 71", 0x47))
    }

    @Test fun parseDtcs07_empty() {
        assertEquals(emptyList(), Elm327Protocol.parseDtcs("47 00", 0x47))
    }

    @Test fun parseDtcs0A_permanentSid() {
        assertEquals(listOf("P0420"), Elm327Protocol.parseDtcs("4A 04 20", 0x4A))
    }

    @Test fun parseDtcs_wrongSidRejected() {
        // A 0x47 pending response must NOT parse as a 0x43 confirmed response.
        assertFailsWith<IllegalArgumentException> {
            Elm327Protocol.parseDtcs("47 01 71") // default SID is 0x43
        }
        assertFailsWith<IllegalArgumentException> {
            Elm327Protocol.parseDtcs("43 01 71", 0x47)
        }
    }

    @Test fun parseDtcs_stopsAtZeroPair() {
        assertEquals(listOf("P0171"), Elm327Protocol.parseDtcs("43 01 71 00 00"))
    }

    // ---- PID decoding (SAE J1979) ----

    @Test fun decodePid_rpm() {
        // ((0x1A*256)+0xF8)/4 = 1726
        assertEquals(1726.0, Elm327Protocol.decodePid(0x0C, "1A F8"))
    }

    @Test fun decodePid_coolant() {
        assertEquals(67.0, Elm327Protocol.decodePid(0x05, "6B"))
    }

    @Test fun decodePid_fuelTrim() {
        assertEquals(0.0, Elm327Protocol.decodePid(0x06, "80"))
    }

    @Test fun decodePid_unsupportedThrows() {
        assertFailsWith<IllegalArgumentException> {
            Elm327Protocol.decodePid(0xFF, "00")
        }
    }

    @Test fun decodePid_shortPayloadThrows() {
        assertFailsWith<IllegalArgumentException> {
            Elm327Protocol.decodePid(0x0C, "1A")
        }
    }

    // ---- sanitize / VIN / commands ----

    @Test fun sanitize_dropsEchoAndNoise() {
        val raw = "0902\rSEARCHING...\r0: 49 02 01 31 46\rOK\r>"
        val lines = Elm327Protocol.sanitize(raw)
        assertEquals(listOf("49 02 01 31 46"), lines)
    }

    @Test fun sanitize_noDataGivesEmpty() {
        assertEquals(emptyList(), Elm327Protocol.sanitize("NO DATA\r>"))
    }

    @Test fun parseVin_multiFrame() {
        val lines = listOf(
            "49 02 01 31 46 4D 43 55 30 47",
            "44 37 4A 55 41 31 32 33 34 35"
        )
        assertEquals("1FMCU0GD7JUA12345", Elm327Protocol.parseVin(lines))
    }

    @Test fun parseVin_numberedFrames() {
        val raw = "0: 49 02 01 31 46 4D\r1: 43 55 30 47 44 37 4A\r2: 55 41 31 32 33 34 35"
        assertEquals("1FMCU0GD7JUA12345", Elm327Protocol.parseVin(Elm327Protocol.sanitize(raw)))
    }

    @Test fun pidCommand_format() {
        assertEquals("010C", Elm327Protocol.pidCommand(0x0C))
        assertEquals("0105", Elm327Protocol.pidCommand(0x05))
    }

    @Test fun initSequence_shape() {
        val seq = Elm327Protocol.initSequence()
        assertEquals("ATZ", seq.first())
        assertTrue(seq.contains("ATE0"))
        assertEquals(6, seq.size)
    }
}
