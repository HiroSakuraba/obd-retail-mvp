package com.glovebox.obd.scan

/**
 * The immutable result of one vehicle scan: identity (VIN), the evidence the
 * ECU reported (DTCs), and the live PID values the diagnostic graphs need.
 * A DTC is evidence, not a parts recommendation — ranking causes is the
 * job of [com.glovebox.obd.diagnosis.DiagnosticEngine].
 */
data class VehicleSession(
    val vin: String?,
    val dtcs: List<String>,
    val pids: Map<String, Double>
)

/** UI state for the scan screen. */
sealed interface ScanState {
    data object Idle : ScanState
    data object Connecting : ScanState
    data object ReadingVehicle : ScanState
    data class Complete(val session: VehicleSession) : ScanState
    data class Error(val message: String) : ScanState
}
