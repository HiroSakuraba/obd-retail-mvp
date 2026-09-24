package com.glovebox.obd.scan

import com.glovebox.obd.ble.ApprovedDongleBleClient

/**
 * Backend contract for the scan step.
 *
 * The long-term contract is measurement recipes: the backend returns a list
 * of *signals* (acquisition recipes), e.g.
 *
 *   { "signal": "o2_upstream_trace",
 *     "source": { "service": "01", "pid": "14" },
 *     "sample_rate_hz": 8, "duration_s": 10,
 *     "conditions": { "rpm_min": 2400, "rpm_max": 2600 },
 *     "derived": ["switch_count", "amplitude"] }
 *
 * served by `GET /v1/sessions/{id}/required-signals` as `{"signals": [...]}`.
 * Sampling a signal over time (the actual measurement protocol) is the job
 * of the future :app module; the :core scan below only needs the PID list,
 * so it derives plain Mode-01 PIDs from the signal sources and falls back
 * to [requiredPidsFor] when the backend returns none.
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
     * Pull plain Mode-01 PID hex strings ("0C") out of measurement-recipe
     * source blocks. Signals whose source is not service 01 (freeze frame,
     * Mode 06 monitors, …) need a real measurement protocol and are not
     * readable as static PIDs — they are skipped here, never zero-filled.
     */
    fun pidsForSignals(signals: List<Map<String, Any>>): List<String> =
        signals.mapNotNull { signal ->
            val source = signal["source"] as? Map<*, *> ?: return@mapNotNull null
            if (source["service"] != "01") return@mapNotNull null
            (source["pid"] as? String)?.uppercase()?.takeIf { it.matches(Regex("[0-9A-F]{2}")) }
        }.distinct()
}
