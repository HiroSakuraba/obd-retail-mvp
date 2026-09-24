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
        val r = DiagnosticEngine(GRAPH).score(emptyMap(), setOf("flashing_mil"))
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
}
