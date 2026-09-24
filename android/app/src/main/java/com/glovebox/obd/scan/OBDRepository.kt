package com.glovebox.obd.scan

import com.glovebox.obd.ble.ApprovedDongleBleClient

/** Backend contract: which live PIDs a set of DTCs needs for diagnosis. */
interface DiagnosticApi {
    suspend fun requiredPidsFor(dtcs: List<String>): List<String>
}

/**
 * Orchestrates one scan: connect, read VIN + DTCs, ask the backend which
 * PIDs the matching diagnostic graphs need, then read exactly those PIDs.
 */
class OBDRepository(
    private val ble: ApprovedDongleBleClient,
    private val diagnosticApi: DiagnosticApi
) {
    suspend fun scan(): VehicleSession {
        ble.connect()
        try {
            val vin = ble.readVin()
            val confirmed = ble.readConfirmedDtcs()
            val pending = ble.readPendingDtcs()
            val requiredPids = diagnosticApi.requiredPidsFor(confirmed + pending)
            val values = ble.readPids(requiredPids)
            return VehicleSession(vin, (confirmed + pending).distinct(), values)
        } finally {
            ble.disconnect()
        }
    }
}
