package com.glovebox.obd.elm

/**
 * ELM327 AT-command protocol: command builders, response sanitizing, and
 * parsers for VIN / DTC / PID responses. Pure Kotlin, no Android imports.
 *
 * This is the read-only half of the allowlist: only the command builders
 * below are ever sent to an adapter (see Elm327Session), so there is no way
 * to construct a raw CAN write through this API.
 */
object Elm327Protocol {

    /** AT init sequence sent once per session, in order. */
    fun initSequence(): List<String> = listOf(
        "ATZ",   // reset the adapter to defaults
        "ATE0",  // echo off: responses contain only ECU data
        "ATL0",  // linefeeds off: single \r line endings
        "ATS0",  // spaces off in ECU data (we re-insert them when parsing)
        "ATH1",  // headers on: keep ECU id + ISO-TP PCI bytes so we can strip them explicitly
        "ATSP0", // protocol auto: let the adapter negotiate 11/29-bit CAN
    )

    /** Service 09 PID 02: vehicle identification number. */
    fun vinCommand() = "0902"

    /** Service 03: confirmed (stored) diagnostic trouble codes. */
    fun dtcCommand() = "03"

    /** Service 07: pending DTCs. */
    fun pendingDtcCommand() = "07"

    /** Service 0A: permanent DTCs. */
    fun permanentDtcCommand() = "0A"

    /** Service 01 PID read, e.g. pidCommand(0x0C) -> "010C". */
    fun pidCommand(pid: Int): String {
        require(pid in 0x00..0xFF) { "PID out of range: $pid" }
        return "01%02X".format(pid)
    }

    /** Service 02 PID 02: freeze-frame snapshot tied to the stored DTC. */
    fun freezeFrameCommand() = "0202"

    /**
     * Strip one raw ELM327 response down to clean uppercase hex token lines.
     * Drops echo, ">" prompts, "SEARCHING...", "NO DATA", "?" and "OK" lines,
     * "N:" ECU line numbering, bare byte-count lines, and CAN-ID + ISO-TP
     * PCI prefixes ("7E8 10 14" / "7E8 21").
     */
    fun sanitize(raw: String): List<String> {
        val out = mutableListOf<String>()
        for (line in raw.lines()) {
            var l = line.trim().trimEnd('>').trim()
            if (l.isEmpty()) continue
            val up = l.uppercase()
            if (up == "NO DATA" || up == "?" || up == "OK"
                || up.startsWith("SEARCHING") || up.startsWith("ELM327")
            ) continue
            l = l.replace(Regex("^\\d+\\s*:"), "").trim() // "0:" / "014:" numbering
            var toks = l.split(Regex("\\s+")).filter { it.isNotEmpty() }
            if (toks.size >= 2
                && toks[0].matches(Regex("[0-9A-Fa-f]{3}"))
                && toks[1].matches(Regex("[12][0-9A-Fa-f]"))
            ) toks = toks.drop(2) // CAN id + ISO-TP PCI byte
            toks = toks.filter { it.matches(Regex("[0-9A-Fa-f]{1,2}")) } // drops echo + count lines
            if (toks.isNotEmpty()) out.add(toks.joinToString(" ").uppercase())
        }
        return out
    }

    /**
     * Reassemble a 17-char VIN from a service-09 multi-frame response.
     * Accepts "014:"-style numbered lines, "7E8 10 14"-style PCI frames, and
     * plain concatenated frames; the "49 02" header is located explicitly
     * and the message-count byte skipped. Throws IllegalArgumentException
     * on anything that does not contain a well-formed VIN.
     */
    fun parseVin(lines: List<String>): String {
        val bytes = lines.flatMap { it.split(" ") }
            .map { it.toIntOrNull(16) ?: throw IllegalArgumentException("non-hex VIN token '$it'") }
        val i = bytes.indexOf(0x49)
        require(i >= 0 && i + 2 < bytes.size && bytes[i + 1] == 0x02) {
            "no '49 02' header in VIN response"
        }
        val vinBytes = bytes.drop(i + 3).take(17) // skip message-count byte, take 17 VIN chars
        require(vinBytes.size == 17) { "VIN payload is ${vinBytes.size} bytes, expected 17" }
        val vin = vinBytes.map { it.toChar() }.joinToString("")
        require(vin.matches(Regex("[A-HJ-NPR-Z0-9]{17}"))) { "VIN '$vin' fails ISO 3779 format check" }
        return vin
    }

    /**
     * Parse a service-03/07/0A DTC response into SAE J2012 DTC strings
     * ("P0171"). The expected positive-response SID must be given:
     * 0x43 for service 03, 0x47 for service 07, 0x4A for service 0A.
     * Stops at the first "00 00" pair; "43 00" yields an empty list.
     * Multi-frame "N:" continuations are handled by [sanitize].
     */
    fun parseDtcs(response: String, responseSid: Int = 0x43): List<String> {
        val bytes = sanitize(response).flatMap { it.split(" ") }.map { it.toInt(16) }
        val i = bytes.indexOf(responseSid)
        require(i >= 0) {
            "no '${responseSid.toString(16).uppercase()}' service header in DTC response"
        }
        val out = mutableListOf<String>()
        var j = i + 1
        while (j + 1 < bytes.size) {
            val b1 = bytes[j]
            val b2 = bytes[j + 1]
            if (b1 == 0 && b2 == 0) break
            out.add(decodeDtc(b1, b2))
            j += 2
        }
        return out
    }

    /** Decode one 2-byte DTC per SAE J2012: bits 7-6 select P/C/B/U. */
    private fun decodeDtc(b1: Int, b2: Int): String {
        val sys = when (b1 shr 6) { 0 -> 'P'; 1 -> 'C'; 2 -> 'B'; else -> 'U' }
        val d2 = "0123"[(b1 shr 4) and 0x03]
        val d3 = "0123456789ABCDEF"[b1 and 0x0F]
        val d4 = "0123456789ABCDEF"[(b2 shr 4) and 0x0F]
        val d5 = "0123456789ABCDEF"[b2 and 0x0F]
        return "$sys$d2$d3$d4$d5"
    }

    /**
     * Decode a service-01 PID payload (the A,B bytes after "41 XX") to a
     * physical value, using the SAE J1979 formula for each supported PID.
     * Throws IllegalArgumentException for unsupported PIDs or short payloads.
     */
    fun decodePid(pid: Int, dataHex: String): Double {
        val b = dataHex.trim().split(Regex("\\s+")).filter { it.isNotEmpty() }
            .map { it.toIntOrNull(16) ?: throw IllegalArgumentException("non-hex PID payload token '$it'") }
        fun need(n: Int) = require(b.size >= n) {
            "PID 0x${pid.toString(16).uppercase()} needs $n payload bytes, got ${b.size}"
        }
        return when (pid) {
            0x04 -> { need(1); b[0] * 100.0 / 255 }              // calculated engine load, %
            0x05 -> { need(1); (b[0] - 40).toDouble() }           // coolant temperature, C
            0x06, 0x07, 0x08, 0x09 -> { need(1); (b[0] - 128) * 100.0 / 128 } // fuel trims, %
            0x0B -> { need(1); b[0].toDouble() }                  // intake manifold pressure, kPa
            0x0C -> { need(2); (b[0] * 256 + b[1]) / 4.0 }        // engine RPM
            0x0D -> { need(1); b[0].toDouble() }                  // vehicle speed, km/h
            0x0E -> { need(1); b[0] / 2.0 - 64 }                  // timing advance, deg
            0x10 -> { need(2); (b[0] * 256 + b[1]) / 100.0 }      // mass air flow, g/s
            0x11 -> { need(1); b[0] * 100.0 / 255 }              // throttle position, %
            0x1F -> { need(2); (b[0] * 256 + b[1]).toDouble() }   // run time since start, s
            0x21 -> { need(2); (b[0] * 256 + b[1]).toDouble() }   // distance with MIL on, km
            0x33 -> { need(1); b[0].toDouble() }                  // barometric pressure, kPa
            else -> throw IllegalArgumentException(
                "unsupported PID 0x${pid.toString(16).uppercase()}")
        }
    }
}
