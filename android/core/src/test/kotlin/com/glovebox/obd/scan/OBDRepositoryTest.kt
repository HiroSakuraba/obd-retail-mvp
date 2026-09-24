package com.glovebox.obd.scan

import com.glovebox.obd.ble.ApprovedDongleBleClient
import kotlinx.coroutines.runBlocking
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertTrue

private class FakeBle(
    var vin: String? = "1FMCU0GD7JUA12345",
    var confirmed: List<String> = listOf("P0171"),
    var pending: List<String> = listOf("P0133"),
    var permanent: List<String> = listOf("P0420"),
    var pidValues: Map<String, Double> = mapOf("0C" to 800.0, "05" to 67.0),
    var connected: Boolean = false,
    var requestedPids: List<String> = emptyList()
) : ApprovedDongleBleClient {
    override suspend fun connect() { connected = true }
    override suspend fun disconnect() { connected = false }
    override suspend fun readVin() = vin
    override suspend fun readConfirmedDtcs() = confirmed
    override suspend fun readPendingDtcs() = pending
    override suspend fun readPermanentDtcs() = permanent
    override suspend fun readPids(pids: List<String>): Map<String, Double> {
        requestedPids = pids
        return pids.associateWith { pidValues[it] ?: Double.NaN }
    }
    override val firmwareVersion = "1.2.0"
    override val capabilities = 0
}

private class FakeApi(
    var pids: List<String> = listOf("0C", "05"),
    var signals: List<Map<String, Any>> = emptyList(),
    var requiredPidsForCalls: Int = 0,
    var lastEvidence: List<DtcEvidence> = emptyList()
) : DiagnosticApi {
    override suspend fun requiredPidsFor(evidence: List<DtcEvidence>): List<String> {
        requiredPidsForCalls++
        lastEvidence = evidence
        return pids
    }
    override suspend fun requiredSignalsFor(evidence: List<DtcEvidence>): List<Map<String, Any>> {
        lastEvidence = evidence
        return signals
    }
}

class OBDRepositoryTest {

    @Test fun scan_keepsDtcStatus() = runBlocking {
        val session = OBDRepository(FakeBle(), FakeApi()).scan()
        assertEquals(
            listOf(
                DtcEvidence("P0171", DtcStatus.CONFIRMED),
                DtcEvidence("P0133", DtcStatus.PENDING),
                DtcEvidence("P0420", DtcStatus.PERMANENT)
            ),
            session.dtcs
        )
    }

    @Test fun scan_apiSeesFullEvidence() = runBlocking {
        val api = FakeApi()
        OBDRepository(FakeBle(), api).scan()
        assertTrue(api.lastEvidence.any { it.status == DtcStatus.PERMANENT })
        assertEquals(3, api.lastEvidence.size)
    }

    @Test fun scan_fallsBackToPidsWhenNoSignals() = runBlocking {
        val ble = FakeBle()
        val api = FakeApi(pids = listOf("0C", "05"), signals = emptyList())
        val session = OBDRepository(ble, api).scan()
        assertEquals(1, api.requiredPidsForCalls)
        assertEquals(listOf("0C", "05"), ble.requestedPids)
        assertEquals(800.0, session.pids["0C"])
    }

    @Test fun scan_derivesPidsFromSignals() = runBlocking {
        val ble = FakeBle()
        val api = FakeApi(signals = listOf(
            mapOf("signal" to "rpm", "source" to mapOf("service" to "01", "pid" to "0c")),
            mapOf("signal" to "coolant", "source" to mapOf("service" to "01", "pid" to "05")),
            mapOf("signal" to "freeze", "source" to mapOf("service" to "02", "pid" to "02"))
        ))
        OBDRepository(ble, api).scan()
        // Only Mode-01 sources become static PID reads; 02/freeze needs a
        // measurement protocol, so it must not be read as a plain PID.
        assertEquals(listOf("0C", "05"), ble.requestedPids)
        assertEquals(0, api.requiredPidsForCalls)
    }

    @Test fun scan_disconnectsAfterwards() = runBlocking {
        val ble = FakeBle()
        OBDRepository(ble, FakeApi()).scan()
        assertFalse(ble.connected)
    }

    @Test fun scan_vinPassedThrough() = runBlocking {
        val session = OBDRepository(FakeBle(vin = "1FMCU0GD7JUA12345"), FakeApi()).scan()
        assertEquals("1FMCU0GD7JUA12345", session.vin)
    }

    @Test fun pidsForSignals_filtersAndNormalizes() {
        val repo = OBDRepository(FakeBle(), FakeApi())
        val signals = listOf(
            mapOf<String, Any>("source" to mapOf("service" to "01", "pid" to "0c")),
            mapOf<String, Any>("source" to mapOf("service" to "01", "pid" to "0C")), // dup
            mapOf<String, Any>("source" to mapOf("service" to "06", "pid" to "01")), // Mode 06
            mapOf<String, Any>("source" to mapOf("service" to "01", "pid" to "ZZ")), // bad hex
            mapOf<String, Any>("no_source" to true)
        )
        assertEquals(listOf("0C"), repo.pidsForSignals(signals))
    }

    @Test fun pidsForSignals_flatRecipeShape() {
        // Current backend /required-signals shape: flat "service" + "pids".
        val repo = OBDRepository(FakeBle(), FakeApi())
        val signals = listOf(
            mapOf<String, Any>(
                "signal" to "fuel_trim_idle_vs_2500",
                "service" to "01",
                "pids" to listOf("06", "07", "0c")
            ),
            mapOf<String, Any>(
                "signal" to "mode06_misfire_counts",
                "service" to "06",
                "pids" to listOf("01") // Mode 06: needs a measurement protocol
            ),
            mapOf<String, Any>(
                "signal" to "warmup_curve",
                "service" to "01",
                "pids" to listOf("05", "ZZ") // bad hex dropped
            )
        )
        assertEquals(listOf("06", "07", "0C", "05"), repo.pidsForSignals(signals))
    }

    @Test fun pidsForSignals_mixedShapesDeduped() {
        val repo = OBDRepository(FakeBle(), FakeApi())
        val signals = listOf(
            mapOf<String, Any>("signal" to "a", "service" to "01", "pids" to listOf("0C")),
            mapOf<String, Any>("signal" to "b", "source" to mapOf("service" to "01", "pid" to "0c")),
            mapOf<String, Any>("signal" to "c", "service" to "01"), // no pids, no source
            mapOf<String, Any>("signal" to "d", "service" to "02", "pids" to listOf("02"))
        )
        assertEquals(listOf("0C"), repo.pidsForSignals(signals))
    }

    @Test fun scan_derivesPidsFromFlatRecipes() = runBlocking {
        val ble = FakeBle()
        val api = FakeApi(signals = listOf(
            mapOf("signal" to "fuel_trim_idle_vs_2500", "service" to "01",
                "pids" to listOf("06", "07", "0C"), "acquired" to false),
            mapOf("signal" to "mode06_misfire_counts", "service" to "06",
                "pids" to emptyList<String>(), "acquired" to false)
        ))
        OBDRepository(ble, api).scan()
        assertEquals(listOf("06", "07", "0C"), ble.requestedPids)
        assertEquals(0, api.requiredPidsForCalls)
    }
}

class VehicleSessionTest {

    @Test fun dtcStatus_values() {
        assertEquals(
            setOf(DtcStatus.CONFIRMED, DtcStatus.PENDING, DtcStatus.PERMANENT),
            DtcStatus.entries.toSet()
        )
    }

    @Test fun dtcCodes_deprecatedAccessor() {
        @Suppress("DEPRECATION")
        val codes = VehicleSession(
            "VIN",
            listOf(DtcEvidence("P0171", DtcStatus.CONFIRMED)),
            emptyMap()
        ).dtcCodes
        assertEquals(listOf("P0171"), codes)
    }
}
