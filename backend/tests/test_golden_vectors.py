"""Parity contract: the golden vectors in tests/golden/diagnostic_vectors.json
must reproduce exactly when re-run through the backend engine.

The Android DiagnosticEngine.kt reads the same JSON in its Gradle tests;
this test pins the backend half of that contract. If backend engine
semantics change intentionally, regenerate the vectors with
backend/app/golden_vectors.py (its invariants will fail first — that is
the point) and update both sides together.
"""
import json
from pathlib import Path

from app import engine

GOLDEN = (Path(__file__).resolve().parent.parent.parent
          / "tests" / "golden" / "diagnostic_vectors.json")


def _load():
    d = json.loads(GOLDEN.read_text())
    assert d["version"] == 1, d
    return d["vectors"]


def _score_primary(v):
    g = engine.GRAPHS[v["expected"]["family"]]
    codes = [d["code"] for d in v["dtcs"]]
    return engine.score(g, v["observations"], set(v["flags"]),
                        dtc_evidence=v["dtcs"], all_dtcs=codes)


def test_golden_file_exists_and_covers_the_semantics():
    names = {v["name"] for v in _load()}
    required = {
        "dedup_cruise_o2_trace_strongest_rule_wins",
        "categorical_negative_no_leak_found",
        "legacy_yes_no_guided_fallback",
        "code_context_p0171_p0174_boosts_vacuum_leak",
        "multi_graph_p0302_p0171_ranked",
        "basket_hose_satisfied_by_leak_found",
        "basket_hose_gated_without_visual_confirmation",
        "stop_if_flashing_mil_halts_scoring",
    }
    assert required <= names, required - names


def test_golden_vectors_reproduce():
    for v in _load():
        scored = _score_primary(v)
        exp = v["expected"]
        assert scored["stopped"] == bool(v.get("expect_stopped")), v["name"]
        top = scored["top"]["id"] if scored["top"] else None
        assert top == exp["top"], v["name"]
        assert scored["confidence"] == exp["confidence"], v["name"]
        scores = {h["id"]: round(h["diagnostic_score"], 6)
                  for h in scored["hypotheses"]}
        assert scores == exp["scores"], v["name"]
        assert len(scored["evidence_deduped"]) == exp["deduped_dropped"], \
            v["name"]
        assert dict(scored["test_outcomes"]) == exp["test_outcomes"], v["name"]
        assert len(scored["code_context_applied"]) == \
            exp["code_context_applied"], v["name"]
        if "expected_ranking" in v:
            codes = [d["code"] for d in v["dtcs"]]
            ranked = engine.score_all(engine.graphs_for_dtcs(codes),
                                      v["observations"], set(v["flags"]),
                                      dtc_evidence=v["dtcs"])
            assert [s["family"] for s in ranked] == v["expected_ranking"], \
                v["name"]
        if "expected_basket" in v:
            g = engine.GRAPHS[v["expected"]["family"]]
            b = engine.repair_basket(g, scored, v["observations"])
            got = {"items": [i["repair_id"] for i in b["items"]],
                   "gated": [x["repair_id"] for x in b["gated"]],
                   "guidance": [x["repair_id"] for x in b["guidance"]]}
            assert got == v["expected_basket"], v["name"]
