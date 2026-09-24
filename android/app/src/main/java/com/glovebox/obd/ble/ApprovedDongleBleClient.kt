package com.glovebox.obd.ble

/**
 * Contract for talking to an approved (allowlisted, signed-firmware) dongle.
 *
 * The implementation speaks the JSON request/response BLE protocol (see
 * firmware/include/ble_protocol.h) and must refuse to pair with a dongle
 * whose firmware version or capability flags fail the security policy.
 * There is intentionally no "write" operation on this interface.
 */
interface ApprovedDongleBleClient {

    /** Connect to the paired dongle and verify firmware/capabilities. */
    suspend fun connect()

    suspend fun disconnect()

    /** VIN via OBD service 09/02, or null if the ECU does not provide it. */
    suspend fun readVin(): String?

    /** Confirmed DTCs (service 03), e.g. ["P0171"]. */
    suspend fun readConfirmedDtcs(): List<String>

    /** Pending DTCs (service 07). */
    suspend fun readPendingDtcs(): List<String>

    /**
     * Read Mode-01 PIDs by two-digit hex string (e.g. "0C"). The dongle
     * enforces its own PID allowlist; PIDs it refuses are simply absent
     * from the returned map.
     */
    suspend fun readPids(pids: List<String>): Map<String, Double>

    /** Firmware version string reported during connect, e.g. "1.2.0". */
    val firmwareVersion: String

    /** Capability bitmask (BLE_CAP_* from ble_protocol.h). */
    val capabilities: Int
}
