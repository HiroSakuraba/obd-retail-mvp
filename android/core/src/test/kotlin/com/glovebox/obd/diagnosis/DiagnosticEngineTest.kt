package com.glovebox.obd.diagnosis

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNull
import kotlin.test.assertTrue

private const val GRAPH = """{
  "safety": {"stop_if": ["flashing_mil"], "stop_message": "Stop driving."},
  "tests": [
    {"id": "t_or", "kind": "obd_live",
     "result_rules": [
       {"when": "x != 0 or y != 0", "updates": {"h_a": 3.0}}
     ]},
    {"id": "t_prec", "kind": "obd_live",
     "result_rules": [
       {"when": "x != 0 or y != 0 and z != 0", "updates": {"h_b": 5.0}}
     ]},
    {"id": "t_visual", "kind": "guided_visual",
     "updates_if_yes": {"h_a": 2.0}}
  ],
  "initial_hypotheses": [
    {"id": "h_a", "label": "Hypothesis A", "prior": 0.5},
    {"id": "h_b", "label": "Hypothesis B", "prior": 0.5}
  ]
}"""

class DiagnosticEngineTest {

    @Test fun orRule_trueLeftSideFires() {
        // Regression: with the old short-circuit evaluator, "x != 0 or y != 0"
        // left the right-side tokens unconsumed and the update never applied.
        val r = DiagnosticEngine(GRAPH).score(mapOf("t_or" to mapOf("x" to 1.0, "y" to 0.0, "z" to 0.0)))
        val top = r.top!!
        assertEquals("h_a", top.id)
        // 0.5*3 = 1.5, total 2.0 -> 0.75; uses diagnostic_score, not posterior.
        assertEquals(0.75, top.diagnostic_score, 1e-9)
    }

    @Test fun andBindsTighterThanOr_notFired() {
        // "x != 0 or y != 0 and z != 0" == x!=0 or (y!=0 and z!=0): all-false LHS, y=0.
        val r = DiagnosticEngine(GRAPH).score(mapOf("t_prec" to mapOf("x" to 0.0, "y" to 0.0, "z" to 1.0)))
        val hb = r.hypotheses.first { it.id == "h_b" }
        assertEquals(0.5, hb.diagnostic_score, 1e-9)
    }

    @Test fun andBindsTighterThanOr_fired() {
        val r = DiagnosticEngine(GRAPH).score(mapOf("t_prec" to mapOf("x" to 0.0, "y" to 1.0, "z" to 1.0)))
        val hb = r.hypotheses.first { it.id == "h_b" }
        assertEquals(2.5 / 3.0, hb.diagnostic_score, 1e-9)
    }

    @Test fun nextTest_firstUnobserved() {
        val e = DiagnosticEngine(GRAPH)
        assertEquals("t_or", e.nextTest(emptySet()))
        assertEquals("t_prec", e.nextTest(setOf("t_or")))
        assertEquals("t_visual", e.nextTest(setOf("t_or", "t_prec")))
        assertNull(e.nextTest(setOf("t_or", "t_prec", "t_visual")))
    }

    @Test fun stopFlag_blocksDiagnosis() {
        val r = DiagnosticEngine(GRAPH).score(
            emptyMap<String, Map<String, Double>>(), setOf("flashing_mil"))
        assertEquals(ConfidenceBand.INSUFFICIENT, r.band)
        assertEquals("Stop driving.", r.stopMessage)
        assertNull(r.top)
    }

    @Test fun guidedVisual_yesAppliesUpdates() {
        val r = DiagnosticEngine(GRAPH).score(mapOf("t_visual" to mapOf("answer" to 1.0)))
        val ha = r.hypotheses.first { it.id == "h_a" }
        assertEquals(1.0 / 1.5, ha.diagnostic_score, 1e-9)
    }

    @Test fun guidedVisual_noAnswerAppliesNothing() {
        val r = DiagnosticEngine(GRAPH).score(mapOf("t_visual" to mapOf("answer" to 0.0)))
        assertTrue(r.hypotheses.all { it.diagnostic_score == 0.5 })
    }

    @Test fun strongBand_threshold() {
        // t_or fires (h_a x3) and visual yes (h_a x2): 3.0/3.5 = 0.857 > 0.75.
        val r = DiagnosticEngine(GRAPH).score(
            mapOf("t_or" to mapOf("x" to 1.0, "y" to 0.0, "z" to 0.0),
                  "t_visual" to mapOf("answer" to 1.0)))
        assertEquals(ConfidenceBand.STRONG, r.band)
        assertEquals(3.0 / 3.5, r.top!!.diagnostic_score, 1e-9)
    }

    // ------------------------------------------------- new engine semantics --

    private val DEDUP_GRAPH = """{
      "safety": {"stop_if": []},
      "tests": [
        {"id": "t_trace", "kind": "obd_live",
         "result_rules": [
           {"when": "a >= 1", "evidence_group": "g1", "updates": {"h_a": 2.0, "h_b": 0.5}},
           {"when": "b >= 1", "evidence_group": "g1", "updates": {"h_a": 3.0, "h_b": 0.9}}
         ]}
      ],
      "initial_hypotheses": [
        {"id": "h_a", "label": "A", "prior": 0.5},
        {"id": "h_b", "label": "B", "prior": 0.5}
      ]
    }"""

    @Test fun dedup_keepsStrongestLogMultiplierPerGroup() {
        // Both rules fire in group g1. h_a: |ln 3| > |ln 2| -> keep 3.0.
        // h_b: |ln 0.5| = 0.693 > |ln 0.9| = 0.105 -> keep 0.5.
        val r = DiagnosticEngine(DEDUP_GRAPH).score(
            mapOf("t_trace" to Observation(mapOf("a" to 1.0, "b" to 1.0))))
        assertEquals(2, r.deduped.size)
        val dropped = r.deduped.map { it.droppedMultiplier }.toSet()
        assertEquals(setOf(2.0, 0.9), dropped)
        val ha = r.hypotheses.first { it.id == "h_a" }
        val hb = r.hypotheses.first { it.id == "h_b" }
        // h_a: 0.5*3=1.5, h_b: 0.5*0.5=0.25, total 1.75.
        assertEquals(1.5 / 1.75, ha.diagnostic_score, 1e-9)
        assertEquals(0.25 / 1.75, hb.diagnostic_score, 1e-9)
    }

    @Test fun dedup_distinctGroupsAreIndependent() {
        val g = DEDUP_GRAPH.replace("\"evidence_group\": \"g1\", \"updates\": {\"h_a\": 3.0",
            "\"evidence_group\": \"g2\", \"updates\": {\"h_a\": 3.0")
        val r = DiagnosticEngine(g).score(
            mapOf("t_trace" to Observation(mapOf("a" to 1.0, "b" to 1.0))))
        assertTrue(r.deduped.isEmpty())
        val ha = r.hypotheses.first { it.id == "h_a" }
        assertEquals(0.5 * 2.0 * 3.0 / (0.5 * 2.0 * 3.0 + 0.5 * 0.5 * 0.9),
            ha.diagnostic_score, 1e-9)
    }

    private val GUIDED_GRAPH = """{
      "safety": {"stop_if": []},
      "tests": [
        {"id": "t_swap", "kind": "guided_manual",
         "outcomes": {
           "follows": {"label": "Follows", "updates": {"h_a": 4.0}},
           "stays": {"label": "Stays", "updates": {"h_a": 0.25}}
         },
         "updates_if_yes": {"h_a": 4.0}}
      ],
      "initial_hypotheses": [
        {"id": "h_a", "label": "A", "prior": 0.5},
        {"id": "h_b", "label": "B", "prior": 0.5}
      ]
    }"""

    @Test fun categoricalOutcome_negativeIsEvidence() {
        val r = DiagnosticEngine(GUIDED_GRAPH).score(
            mapOf("t_swap" to Observation(emptyMap(), "stays")))
        assertEquals("stays", r.testOutcomes["t_swap"])
        val ha = r.hypotheses.first { it.id == "h_a" }
        assertEquals(0.125 / 0.625, ha.diagnostic_score, 1e-9)
    }

    @Test fun categoricalOutcome_unknownFallsBackToAnswer() {
        // "yes" is not in the outcomes map -> legacy updates_if_yes path.
        val r = DiagnosticEngine(GUIDED_GRAPH).score(
            mapOf("t_swap" to Observation(mapOf("answer" to 1.0), "yes")))
        assertEquals("yes", r.testOutcomes["t_swap"])
        val ha = r.hypotheses.first { it.id == "h_a" }
        assertEquals(2.0 / 2.5, ha.diagnostic_score, 1e-9)
    }

    private val CTX_GRAPH = """{
      "safety": {"stop_if": []},
      "dtcs": ["P0001"],
      "code_context": {
        "boost_if_present": [
          {"dtc": "P0002", "hypothesis": "h_a", "multiplier": 2.0}],
        "reduce_if_present": []
      },
      "tests": [],
      "initial_hypotheses": [
        {"id": "h_a", "label": "A", "prior": 0.5},
        {"id": "h_b", "label": "B", "prior": 0.5}
      ]
    }"""

    @Test fun codeContext_boostAppliesWhenCompanionPresent() {
        val e = DiagnosticEngine(CTX_GRAPH)
        val withCtx = e.score(emptyMap(),
            dtcEvidence = listOf(
                com.glovebox.obd.scan.DtcEvidence("P0001",
                    com.glovebox.obd.scan.DtcStatus.CONFIRMED),
                com.glovebox.obd.scan.DtcEvidence("P0002",
                    com.glovebox.obd.scan.DtcStatus.CONFIRMED)))
        assertEquals(1, withCtx.codeContextApplied.size)
        assertEquals(1.0 / 1.5, withCtx.top!!.diagnostic_score, 1e-9)

        val withoutCtx = e.score(emptyMap(),
            dtcEvidence = listOf(
                com.glovebox.obd.scan.DtcEvidence("P0001",
                    com.glovebox.obd.scan.DtcStatus.CONFIRMED)))
        assertTrue(withoutCtx.codeContextApplied.isEmpty())
        assertEquals(0.5, withoutCtx.top!!.diagnostic_score, 1e-9)
    }

    @Test fun graphSet_graphsForDtcs_ordersByMatchCount() {
        val g1 = CTX_GRAPH.replace("\"P0001\"", "\"P0001\"")
        val g2 = CTX_GRAPH.replace("\"family\": \"x\"", "\"family\": \"x\"")
            .replace("\"dtcs\": [\"P0001\"]", "\"dtcs\": [\"P0001\", \"P0002\"]")
        // Build two graphs with different dtc coverage via family injection.
        val set = GraphSet(listOf(
            g1.replaceFirst("{", "{\"family\": \"FAM_ONE\","),
            g2.replaceFirst("{", "{\"family\": \"FAM_TWO\",")
        ))
        val matched = set.graphsForDtcs(listOf("P0001", "P0002"))
        assertEquals(listOf("FAM_TWO", "FAM_ONE"), matched.map { it.family })
        // Both graphs contain P0001: tie on match count falls back to family.
        assertEquals(listOf("FAM_ONE", "FAM_TWO"),
            set.graphsForDtcs(listOf("P0001")).map { it.family })
        assertTrue(set.graphsForDtcs(listOf("P9999")).isEmpty())
    }

    @Test fun graphSet_repairBasket_gatesOnMissingEvidence() {
        val g = """{
          "safety": {"stop_if": []},
          "tests": [{"id": "t1", "kind": "guided_visual",
                     "outcomes": {"found": {"updates": {"h_a": 9.0}}},
                     "updates_if_yes": {"h_a": 9.0}}],
          "initial_hypotheses": [
            {"id": "h_a", "label": "A", "prior": 0.5},
            {"id": "h_b", "label": "B", "prior": 0.5}
          ],
          "repairs": [
            {"repair_id": "r-part", "hypothesis": "h_a", "label": "Replace part",
             "recommendation_class": "replacement", "min_confidence": "moderate",
             "requires_any": ["t1:found"]},
            {"repair_id": "r-check", "hypothesis": "h_a", "label": "Inspect first",
             "recommendation_class": "diagnostic", "min_confidence": "moderate",
             "requires_any": []}
          ]
        }"""
        val set = GraphSet(listOf(g.replaceFirst("{", "{\"family\": \"F\", \"dtcs\": [\"P1\"],")))
        val graph = set.graphsForDtcs(listOf("P1")).first()

        // Confirming evidence recorded -> part offered, guidance alongside.
        val obsYes = mapOf("t1" to Observation(emptyMap(), "found"))
        val scoredYes = set.scoreAll(listOf(graph), obsYes, emptySet(),
            listOf(com.glovebox.obd.scan.DtcEvidence("P1",
                com.glovebox.obd.scan.DtcStatus.CONFIRMED))).first()
        val basketYes = set.repairBasket(graph, scoredYes.result, obsYes)
        assertEquals(listOf("r-part"), basketYes.items.map { it.repairId })
        assertEquals(listOf("r-check"), basketYes.guidance.map { it.repairId })
        assertTrue(basketYes.gated.isEmpty())

        // Band reached via a different observation that lacks t1:found ->
        // part gated, guidance still shown.
        val obsOther = mapOf("t1" to Observation(mapOf("answer" to 1.0)))
        val scoredOther = set.scoreAll(listOf(graph), obsOther, emptySet(),
            listOf(com.glovebox.obd.scan.DtcEvidence("P1",
                com.glovebox.obd.scan.DtcStatus.CONFIRMED))).first()
        val basketOther = set.repairBasket(graph, scoredOther.result, obsOther)
        assertTrue(basketOther.items.isEmpty())
        assertEquals(listOf("r-part"), basketOther.gated.map { it.repairId })
        assertEquals(listOf("r-check"), basketOther.guidance.map { it.repairId })
    }
}
