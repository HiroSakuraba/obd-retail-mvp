package com.glovebox.obd.elm

/**
 * OBD session over a commodity ELM327-compatible BLE adapter (Veepeak,
 * Carista, VGate-class). Pure Kotlin, no Android imports: the Android app
 * implements [BleGatt] with BluetoothGatt (see android/elm/README.md).
 *
 * Read-only, application-enforced: the only strings ever written to the
 * adapter are [Elm327Protocol.initSequence] plus the five command builders.
 * There is no API to send an arbitrary AT command, so a raw CAN write
 * cannot be expressed through this class — but a *different* app talking to
 * the same commodity adapter is not bound by this. Only the custom dongle
 * (device-enforced allowlist) is safe against a hostile phone app.
 */

// De-facto standard Nordic UART UUIDs used by most BLE OBD adapters.
// RX = phone -> adapter (write), TX = adapter -> phone (notify).
const val UART_SERVICE_UUID = "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"
const val UART_RX_CHAR_UUID = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"
const val UART_TX_CHAR_UUID = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"

/** Minimal BLE GATT contract a session needs; implemented by the Android app. */
interface BleGatt {
    /** Connect and discover services. */
    suspend fun connect()

    /** Disconnect and release resources. */
    suspend fun disconnect()

    val isConnected: Boolean

    /** Write-without-response to the RX characteristic. */
    suspend fun write(data: ByteArray)

    /** Subscribe to TX characteristic notifications. */
    fun onNotify(handler: (ByteArray) -> Unit)
}

/** Thrown when the adapter does not answer with a ">" prompt in time. */
class ElmTimeoutException(message: String) : Exception(message)

/** One OBD session: init once, then read VIN / DTCs / PIDs through the allowlist. */
class Elm327Session(
    private val gatt: BleGatt,
    private val responseTimeoutMs: Long = 5000,
) {
    private val rx = StringBuilder()

    /** Run the AT init sequence; must be called before any read. */
    suspend fun initialize() {
        gatt.connect()
        gatt.onNotify { chunk ->
            synchronized(rx) {
                rx.append(chunk.toString(Charsets.US_ASCII))
                (rx as java.lang.Object).notifyAll()
            }
        }
        for (cmd in Elm327Protocol.initSequence()) transact(cmd)
    }

    /** Read the 17-char VIN via service 09 PID 02. */
    suspend fun readVin(): String =
        Elm327Protocol.parseVin(Elm327Protocol.sanitize(transact(Elm327Protocol.vinCommand())))

    /** Read confirmed DTCs via service 03. */
    suspend fun readDtcs(): List<String> =
        Elm327Protocol.parseDtcs(transact(Elm327Protocol.dtcCommand()))

    /** Read pending DTCs via service 07 (positive-response SID 0x47). */
    suspend fun readPendingDtcs(): List<String> =
        Elm327Protocol.parseDtcs(transact(Elm327Protocol.pendingDtcCommand()), 0x47)

    /** Read permanent DTCs via service 0A (positive-response SID 0x4A). */
    suspend fun readPermanentDtcs(): List<String> =
        Elm327Protocol.parseDtcs(transact(Elm327Protocol.permanentDtcCommand()), 0x4A)

    /**
     * Read service-01 PIDs, keyed by two-digit hex string ("0C" -> rpm).
     * PIDs the ECU does not support ("NO DATA" / missing "41 XX" header)
     * are skipped, not failed: the caller decides whether a missing PID
     * blocks its diagnostic graph.
     */
    suspend fun readPids(pids: List<Int>): Map<String, Double> {
        val out = linkedMapOf<String, Double>()
        for (pid in pids) {
            val bytes = Elm327Protocol.sanitize(transact(Elm327Protocol.pidCommand(pid)))
                .flatMap { it.split(" ") }.map { it.toInt(16) }
            val i = bytes.indexOf(0x41)
            if (i < 0 || i + 1 >= bytes.size || bytes[i + 1] != pid) continue
            val payload = bytes.drop(i + 2).joinToString(" ") { "%02X".format(it) }
            out["%02X".format(pid)] = Elm327Protocol.decodePid(pid, payload)
        }
        return out
    }

    /** End the session and disconnect. */
    suspend fun close() = gatt.disconnect()

    /** Write a command and block until the ">" prompt terminates the response. */
    private suspend fun transact(command: String): String {
        gatt.write((command + "\r").toByteArray(Charsets.US_ASCII))
        return awaitPrompt()
    }

    private fun awaitPrompt(): String {
        val deadline = System.currentTimeMillis() + responseTimeoutMs
        synchronized(rx) {
            while (true) {
                val idx = rx.indexOf(">")
                if (idx >= 0) {
                    val out = rx.substring(0, idx)
                    rx.delete(0, idx + 1)
                    return out
                }
                val left = deadline - System.currentTimeMillis()
                if (left <= 0) throw ElmTimeoutException(
                    "no '>' prompt from adapter within ${responseTimeoutMs}ms")
                (rx as java.lang.Object).wait(left)
            }
        }
    }
}
