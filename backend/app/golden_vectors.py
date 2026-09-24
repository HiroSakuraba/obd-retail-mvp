"""Golden diagnostic vectors: the enforced parity contract between the
backend engine (backend/app/engine.py) and the Android DiagnosticEngine.kt.

Run:  cd <repo> && .venv/bin/python backend/app/golden_vectors.py

It scores a fixed set of representative cases through the real backend
engine and writes tests/golden/diagnostic_vectors.json. Both engines are
supposed to agree on these vectors; the Kotlin side reads the same JSON in
Gradle. If backend engine behavior changes, the generator's invariants
fail loudly INSTEAD of silently rewriting the contract — regenerate only
when the behavior change is intended and reviewed.

Vector schema:
  {"version": 1,
   "vectors": [{"name", "families", "dtcs": [{"code","status"}],
                "observations": {test_id: {"fields": {...}, "outcome"?: str}},
                "flags": [],
                "expected": {"family", "top", "confidence",
                             "scores": {hypothesis_id: 6dp},
                             "deduped_dropped": n,
                             "test_outcomes": {},
                             "code_context_applied": n},
                ...optional: "expected_ranking", "expected_basket",
                "expect_stopped"}]}
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Importable both as `python backend/app/golden_vectors.py` from the repo
# root and as a module: put <repo>/backend on sys.path.
_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT / "backend"))

from app import engine  # noqa: E402

OUT = _REPO_ROOT / "tests" / "golden" / "diagnostic_vectors.json"
VERSION = 1


def _ev(codes):
    return [{"code": c, "status": "confirmed"} for c in codes]


def _expected_single(family: str, scored: dict) -> dict:
    return {
        "family": family,
        "top": scored["top"]["id"] if scored["top"] else None,
        "confidence": scored["confidence"],
        "scores": {h["id"]: round(h["diagnostic_score"], 6)
                   for h in scored["hypotheses"]},
        "deduped_dropped": len(scored["evidence_deduped"]),
        "test_outcomes": dict(scored["test_outcomes"]),
        "code_context_applied": len(scored["code_context_applied"]),
    }


def _single(name, family, dtcs, observations, flags=(), invariants=()):
    g = engine.GRAPHS[family]
    codes = [d["code"] for d in dtcs]
    scored = engine.score(g, observations, set(flags), dtc_evidence=dtcs,
                          all_dtcs=codes)
    for inv in invariants:
        inv(name, scored)
    return {
        "name": name,
        "families": [family],
        "dtcs": dtcs,
        "observations": observations,
        "flags": list(flags),
        "expected": _expected_single(family, scored),
    }


# ------------------------------------------------- invariant helpers ---------
def _top_is(hid):
    def check(name, scored):
        assert scored["top"] and scored["top"]["id"] == hid, \
            f"{name}: top {scored['top']}"
    return check


def _confidence_is(band):
    def check(name, scored):
        assert scored["confidence"] == band, \
            f"{name}: confidence {scored['confidence']}"
    return check


def _dedup_at_least(n):
    def check(name, scored):
        assert len(scored["evidence_deduped"]) >= n, \
            f"{name}: deduped {len(scored['evidence_deduped'])}"
    return check


def _code_context_applied(n):
    def check(name, scored):
        assert len(scored["code_context_applied"]) == n, \
            f"{name}: code_context {scored['code_context_applied']}"
    return check


def _test_outcome_is(tid, outcome):
    def check(name, scored):
        assert scored["test_outcomes"].get(tid) == outcome, \
            f"{name}: outcomes {scored['test_outcomes']}"
    return check


# ------------------------------------------------------------------ cases ---
def build_vectors() -> list[dict]:
    vectors = []

    # (a) evidence_group dedup: cruise_o2_trace matches two rules in the
    # SAME evidence_group (switch_ratio>=0.75 and downstream_swing_v>=0.5);
    # per hypothesis only the strongest |log multiplier| may count.
    vectors.append(_single(
        "dedup_cruise_o2_trace_strongest_rule_wins",
        "P0420_CATALYST_EFFICIENCY_BANK1", _ev(["P0420"]),
        {"cruise_o2_trace": {"fields": {"switch_ratio": 0.85,
                                        "downstream_swing_v": 0.6}}},
        invariants=[_top_is("failing_catalytic_converter"),
                    _confidence_is("moderate"),
                    _dedup_at_least(1)],
    ))

    # (b) categorical negative guided outcome: a clean intake inspection
    # down-weights vacuum_leak instead of contributing nothing.
    vectors.append(_single(
        "categorical_negative_no_leak_found",
        "P0171_LEAN_BANK_1", _ev(["P0171"]),
        {"fuel_trim_idle_vs_2500": {"fields": {"trim_idle": 33.0,
                                               "trim_2500": 12.0}},
         "inspect_intake": {"fields": {}, "outcome": "no_leak_found"}},
        invariants=[_top_is("vacuum_leak"),
                    _confidence_is("insufficient"),
                    _test_outcome_is("inspect_intake", "no_leak_found")],
    ))

    # (c) legacy yes/no guided fallback: flat {"answer": 1.0} hits
    # updates_if_yes when no categorical outcome is recorded.
    vectors.append(_single(
        "legacy_yes_no_guided_fallback",
        "P0420_CATALYST_EFFICIENCY_BANK1", _ev(["P0420"]),
        {"exhaust_leak_check": {"fields": {"answer": 1.0}}},
        invariants=[_top_is("exhaust_leak"),
                    _test_outcome_is("exhaust_leak_check", "yes")],
    ))

    # (d) code_context boost: P0171+P0174 boosts the common-cause
    # vacuum_leak hypothesis; the boost entry is recorded.
    vectors.append(_single(
        "code_context_p0171_p0174_boosts_vacuum_leak",
        "P0171_LEAN_BANK_1", _ev(["P0171", "P0174"]), {},
        invariants=[_top_is("vacuum_leak"),
                    _code_context_applied(1)],
    ))

    # (e) multi-graph: codes spanning two families are ALL scored and
    # ranked; expected_ranking is the score_all order.
    dtcs = _ev(["P0302", "P0171"])
    codes = [d["code"] for d in dtcs]
    ranked = engine.score_all(engine.graphs_for_dtcs(codes), {},
                              set(), dtc_evidence=dtcs)
    ranking = [s["family"] for s in ranked]
    assert ranking == ["P0171_LEAN_BANK_1", "P030X_MISFIRE"], ranking
    vectors.append({
        "name": "multi_graph_p0302_p0171_ranked",
        "families": ["P0171_LEAN_BANK_1", "P030X_MISFIRE"],
        "dtcs": dtcs,
        "observations": {},
        "flags": [],
        "expected": _expected_single(ranking[0], ranked[0]),
        "expected_ranking": ranking,
    })

    # (f1) basket satisfied: visual leak confirmation unlocks the hose.
    obs_sat = {"fuel_trim_idle_vs_2500": {"fields": {"trim_idle": 33.0,
                                                     "trim_2500": 12.0}},
               "inspect_intake": {"fields": {}, "outcome": "leak_found"}}
    g = engine.GRAPHS["P0171_LEAN_BANK_1"]
    scored_sat = engine.score(g, obs_sat, set(), dtc_evidence=_ev(["P0171"]),
                              all_dtcs=["P0171"])
    b_sat = engine.repair_basket(g, scored_sat, obs_sat)
    assert any(i["repair_id"] == "replace-intake-pcv-hose"
               for i in b_sat["items"]), b_sat
    vectors.append({
        "name": "basket_hose_satisfied_by_leak_found",
        "families": ["P0171_LEAN_BANK_1"],
        "dtcs": _ev(["P0171"]),
        "observations": obs_sat,
        "flags": [],
        "expected": _expected_single("P0171_LEAN_BANK_1", scored_sat),
        "expected_basket": {
            "items": [i["repair_id"] for i in b_sat["items"]],
            "gated": [x["repair_id"] for x in b_sat["gated"]],
            "guidance": [x["repair_id"] for x in b_sat["guidance"]],
        },
    })

    # (f2) basket gated: trim evidence alone scores vacuum_leak moderate
    # but the hose stays gated on the missing visual confirmation.
    obs_gate = {"fuel_trim_idle_vs_2500": {"fields": {"trim_idle": 33.0,
                                                      "trim_2500": 12.0}}}
    scored_gate = engine.score(g, obs_gate, set(), dtc_evidence=_ev(["P0171"]),
                               all_dtcs=["P0171"])
    b_gate = engine.repair_basket(g, scored_gate, obs_gate)
    assert b_gate["items"] == [], b_gate
    assert any(x["repair_id"] == "replace-intake-pcv-hose"
               for x in b_gate["gated"]), b_gate
    vectors.append({
        "name": "basket_hose_gated_without_visual_confirmation",
        "families": ["P0171_LEAN_BANK_1"],
        "dtcs": _ev(["P0171"]),
        "observations": obs_gate,
        "flags": [],
        "expected": _expected_single("P0171_LEAN_BANK_1", scored_gate),
        "expected_basket": {
            "items": [i["repair_id"] for i in b_gate["items"]],
            "gated": [x["repair_id"] for x in b_gate["gated"]],
            "guidance": [x["repair_id"] for x in b_gate["guidance"]],
        },
    })

    # (g) stop_if: a safety flag stops scoring entirely.
    g = engine.GRAPHS["P0171_LEAN_BANK_1"]
    scored_stop = engine.score(g, {}, {"flashing_mil"},
                               dtc_evidence=_ev(["P0171"]),
                               all_dtcs=["P0171"])
    assert scored_stop["stopped"] is True, scored_stop
    vectors.append({
        "name": "stop_if_flashing_mil_halts_scoring",
        "families": ["P0171_LEAN_BANK_1"],
        "dtcs": _ev(["P0171"]),
        "observations": {},
        "flags": ["flashing_mil"],
        "expected": _expected_single("P0171_LEAN_BANK_1", scored_stop),
        "expect_stopped": True,
    })

    return vectors


def main() -> Path:
    vectors = build_vectors()
    data = {"version": VERSION, "vectors": vectors}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, indent=2) + "\n")
    print(f"wrote {OUT} ({len(vectors)} vectors)")
    return OUT


if __name__ == "__main__":
    main()
