package com.glovebox.obd.scan

/**
 * Diagnostic status of one trouble code: confirmed (service 03), pending
 * (service 07), or permanent (service 0A). These mean different things —
 * a pending code is an unconfirmed suspicion, a permanent code survives
 * code clears until the monitor runs clean — so the status travels with
 * the code all the way into the diagnostic engine.
 */
enum class DtcStatus { CONFIRMED, PENDING, PERMANENT }

/** One trouble code plus the service it was read from. */
data class DtcEvidence(val code: String, val status: DtcStatus)

/**
 * The immutable result of one vehicle scan: identity (VIN), the evidence the
 * ECU reported (DTCs with status), and the live PID values the diagnostic
 * graphs need. A DTC is evidence, not a parts recommendation — ranking
 * causes is the job of [com.glovebox.obd.diagnosis.DiagnosticEngine].
 */
data class VehicleSession(
    val vin: String?,
    val dtcs: List<DtcEvidence>,
    val pids: Map<String, Double>
) {
    /** Bare codes, for graph lookup. Prefer [dtcs] when status matters. */
    @Deprecated("Use dtcs and read each entry's status", ReplaceWith("dtcs.map { it.code }"))
    val dtcCodes: List<String> get() = dtcs.map { it.code }
}

/** UI state for the scan screen. */
sealed interface ScanState {
    data object Idle : ScanState
    data object Connecting : ScanState
    data object ReadingVehicle : ScanState
    data class Complete(val session: VehicleSession) : ScanState
    data class Error(val message: String) : ScanState
}
