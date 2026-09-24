package com.glovebox.obd.diagnosis

import com.glovebox.obd.scan.DtcEvidence
import com.glovebox.obd.scan.DtcStatus
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNotNull
import kotlin.test.assertNull
import kotlin.test.assertTrue

/**
 * Parity contract: the golden vectors in
 * tests/golden/diagnostic_vectors.json are generated from the canonical
 * backend engine (backend/app/engine.py) by
 * tests/golden/gen_diagnostic_vectors.py. This test replays every vector
 * through the Kotlin port and asserts identical results (scores to 1e-6).
 */
class GoldenVectorsTest {

    private fun resourceText(path: String): String =
        (javaClass.classLoader.getResourceAsStream(path)
            ?: error("test resource not found: $path"))
            .bufferedReader().readText()

    private val graphSet: GraphSet by lazy {
        val names = listOf(
            "p0171.json", "p030x.json", "p0128.json",
            "p0133.json", "p0420.json", "p0442.json"
        )
        GraphSet(names.map { resourceText("golden/graphs/$it") })
    }

    private val vectors: List<Json.Obj> by lazy {
        (parseJsonDocument(resourceText("golden/diagnostic_vectors.json")) as Json.Obj)
            .reqArr("vectors").map { it as Json.Obj }
    }

    @Test fun goldenVectors_allMatchBackend() {
        assertTrue(vectors.isNotEmpty(), "no golden vectors loaded")
        for (v in vectors) checkVector(v)
    }

    private fun checkVector(v: Json.Obj) {
        val name = v.reqStr("name")
        fun msg(what: String) = "vector '$name': $what"

        val dtcEvidence = v.reqArr("dtcs").map { d ->
            d as Json.Obj
            DtcEvidence(
                d.reqStr("code"),
                DtcStatus.valueOf(d.reqStr("status").uppercase())
            )
        }
        val observations = (v.reqObj("observations").map).mapValues { (_, o) ->
            Observation.fromJson(o)
        }
        val flags = v.reqArr("flags").map { (it as Json.Str).value }.toSet()

        val codes = dtcEvidence.map { it.code }
        val graphs = graphSet.graphsForDtcs(codes)
        assertTrue(graphs.isNotEmpty(), msg("no graph matched $codes"))

        val scored = graphSet.scoreAll(graphs, observations, flags, dtcEvidence)
        val expected = v.reqObj("expected")
        val primary = scored.first()

        assertEquals(v.reqBool("expect_stopped"), primary.result.stopped,
            msg("stopped flag"))

        assertEquals(expected.reqStr("family"), primary.graph.family,
            msg("primary family"))
        val expectedTop = expected.optStr("top")
        if (expectedTop == null) {
            assertNull(primary.result.top, msg("expected no top hypothesis"))
        } else {
            assertEquals(expectedTop, primary.result.top?.id, msg("top hypothesis"))
        }
        assertEquals(expected.reqStr("confidence"),
            primary.result.band.name.lowercase(), msg("confidence band"))

        val expectedScores = expected.reqObj("scores").map
            .mapValues { (it.value as Json.Num).value }
        val actualScores = primary.result.hypotheses.associate { it.id to it.diagnostic_score }
        assertEquals(expectedScores.keys, actualScores.keys, msg("hypothesis ids"))
        for ((hid, want) in expectedScores) {
            assertEquals(want, actualScores.getValue(hid), 1e-6, msg("score of $hid"))
        }

        assertEquals(
            (expected.optNum("deduped_dropped") ?: 0.0).toInt(),
            primary.result.deduped.size,
            msg("deduped record count")
        )
        assertEquals(
            (expected.optNum("code_context_applied") ?: 0.0).toInt(),
            primary.result.codeContextApplied.size,
            msg("code_context applied count")
        )

        val expectedOutcomes = expected.reqObj("test_outcomes").map
            .mapValues { (it.value as? Json.Str)?.value }
        assertEquals(expectedOutcomes.keys, primary.result.testOutcomes.keys,
            msg("test_outcomes keys"))
        for ((tid, want) in expectedOutcomes) {
            assertEquals(want, primary.result.testOutcomes[tid], msg("outcome of $tid"))
        }

        (v.optArr("expected_ranking"))?.let { ranking ->
            assertEquals(
                ranking.map { (it as Json.Str).value },
                scored.map { it.graph.family },
                msg("multi-graph ranking")
            )
        }

        (v.optObj("expected_basket"))?.let { basket ->
            val repairBasket = graphSet.repairBasket(
                primary.graph, primary.result, observations)
            assertNotNull(repairBasket, msg("basket"))
            for ((key, actual) in listOf(
                "items" to repairBasket.items.map { it.repairId },
                "gated" to repairBasket.gated.map { it.repairId },
                "guidance" to repairBasket.guidance.map { it.repairId }
            )) {
                assertEquals(
                    basket.reqArr(key).map { (it as Json.Str).value },
                    actual,
                    msg("basket $key")
                )
            }
        }
    }
}
