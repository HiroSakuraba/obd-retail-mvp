package com.glovebox.obd.scan

import com.glovebox.obd.ble.ApprovedDongleBleClient

/**
 * Backend contract for the scan step.
 *
 * The contract is measurement recipes: the backend returns a list
 * of *signals* (acquisition recipes), e.g.
 *
 *   { "signal": "fuel_trim_idle_vs_2500",
 *     "service": "01", "pids": ["06", "07", "0C"],
 *     "sample_rate_hz": 1, "duration_s": 5,
 *     "conditions": { ... },
 *     "derived": ["trim_idle", "trim_2500"],
 *     "test_id": "fuel_trim_idle_vs_2500", "family": "P0171_LEAN_BANK_1",
 *     "acquired": false }
 *
 * served by `GET /v1/sessions/{id}/required-signals` as `{"signals": [...]}`.
 * Sampling a signal over time (the actual measurement protocol) is the job
 * of the future :app module; the :core scan below only needs the PID list,
 * so it derives plain Mode-01 PIDs from the recipe's flat "service"/"pids"
 * fields and falls back to [requiredPidsFor] when the backend returns none.
 *
 * Legacy shape (still accepted as a fallback): a nested source block,
 *   { "signal": ..., "source": { "service": "01", "pid": "14" }, ... }
 * with a single "pid" string.
 *
 * HTTP wiring is pending the :app module — this interface is implemented
 * against the JSON shape above so the contract is pinned down now.
 */
interface DiagnosticApi {
    /** Legacy contract: which live Mode-01 PIDs a DTC evidence set needs. */
    suspend fun requiredPidsFor(evidence: List<DtcEvidence>): List<String>

    /** Measurement recipes the matching diagnostic graphs need. */
    suspend fun requiredSignalsFor(evidence: List<DtcEvidence>): List<Map<String, Any>>
}

/**
 * Orchestrates one scan: connect, read VIN + DTCs (confirmed, pending and
 * permanent — each kept with its status), ask the backend which signals the
 * matching diagnostic graphs need, derive the Mode-01 PIDs from those
 * recipes (falling back to [DiagnosticApi.requiredPidsFor]), then read
 * exactly those PIDs.
 */
class OBDRepository(
    private val ble: ApprovedDongleBleClient,
    private val diagnosticApi: DiagnosticApi
) {
    suspend fun scan(): VehicleSession {
        ble.connect()
        try {
            val vin = ble.readVin()
            val evidence = ble.readConfirmedDtcs().map { DtcEvidence(it, DtcStatus.CONFIRMED) } +
                    ble.readPendingDtcs().map { DtcEvidence(it, DtcStatus.PENDING) } +
                    ble.readPermanentDtcs().map { DtcEvidence(it, DtcStatus.PERMANENT) }
            val signals = diagnosticApi.requiredSignalsFor(evidence)
            val pidsFromSignals = pidsForSignals(signals)
            val requiredPids =
                if (pidsFromSignals.isNotEmpty()) pidsFromSignals
                else diagnosticApi.requiredPidsFor(evidence)
            val values = ble.readPids(requiredPids)
            return VehicleSession(vin, evidence.distinct(), values)
        } finally {
            ble.disconnect()
        }
    }

    /**
     * Pull plain Mode-01 PID hex strings ("0C") out of measurement recipes.
     *
     * Current recipe shape (flat): "service" + "pids" list, e.g.
     *   {"signal": "fuel_trim_idle_vs_2500", "service": "01",
     *    "pids": ["06", "07", "0C"], ...}
     * Legacy shape (fallback): a nested source block with a single pid,
     *   {"signal": ..., "source": {"service": "01", "pid": "14"}, ...}
     *
     * Signals whose service is not 01 (freeze frame, Mode 06 monitors, …)
     * need a real measurement protocol and are not readable as static PIDs
     * — they are skipped here, never zero-filled.
     */
    fun pidsForSignals(signals: List<Map<String, Any>>): List<String> {
        val out = mutableListOf<String>()
        for (signal in signals) {
            val service: String?
            val pids: List<String>
            @Suppress("UNCHECKED_CAST")
            if (signal["service"] is String && signal["pids"] is List<*>) {
                // Current flat recipe shape.
                service = signal["service"] as String
                pids = (signal["pids"] as List<*>).filterIsInstance<String>()
            } else {
                // Legacy nested source block.
                val source = signal["source"] as? Map<*, *> ?: continue
                service = source["service"] as? String ?: continue
                pids = listOfNotNull(source["pid"] as? String)
            }
            if (service != "01") continue
            for (pid in pids) {
                val norm = pid.uppercase()
                if (norm.matches(Regex("[0-9A-F]{2}")) && norm !in out) out += norm
            }
        }
        return out
    }
}
