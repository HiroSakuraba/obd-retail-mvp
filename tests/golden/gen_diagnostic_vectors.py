#!/usr/bin/env python3
"""Generate tests/golden/diagnostic_vectors.json from the canonical backend.

The backend engine (backend/app/engine.py) is the parity contract: this
script pins its behavior into golden vectors that the Kotlin port
(android/.../diagnosis/GoldenVectorsTest.kt) must reproduce exactly.

Run with the repo venv:  ~/workspace/obd-retail-mvp/.venv/bin/python \
    tests/golden/gen_diagnostic_vectors.py

Outputs:
  tests/golden/diagnostic_vectors.json                      (repo contract)
  android/core/src/test/resources/golden/diagnostic_vectors.json
  android/core/src/test/resources/golden/graphs/<family>.json  (the 6 graphs)
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "backend"))
from app import engine  # noqa: E402

GRAPH_FILES = ["p0171.json", "p030x.json", "p0128.json",
               "p0133.json", "p0420.json", "p0442.json"]


def dtc(code, status="confirmed"):
    return {"code": code, "status": status}


def obs_flat(tid, fields):
    return {tid: {"fields": dict(fields)}}


def obs_outcome(tid, outcome, fields=None):
    return {tid: {"fields": dict(fields or {}), "outcome": outcome}}


VECTORS = [
    # --- evidence_group dedup: two rules, one measurement, one group ------
    dict(name="dedup_cruise_o2_trace_strongest_rule_wins",
         dtcs=[dtc("P0420")],
         observations=obs_flat("cruise_o2_trace",
                               {"switch_ratio": 0.8, "downstream_swing_v": 0.6}),
         flags=[],
         note="rules switch_ratio>=0.75 and downstream_swing_v>=0.5 both fire "
              "in group cruise_o2_trace; per hypothesis only the stronger "
              "|ln m| survives (2 records dropped)"),
    # --- dedup + code_context on the same pass -----------------------------
    dict(name="dedup_plus_code_context_reduce",
         dtcs=[dtc("P0420"), dtc("P0171")],
         observations=obs_flat("cruise_o2_trace",
                               {"switch_ratio": 0.8, "downstream_swing_v": 0.6}),
         flags=[],
         note="same as above, but P0171 also present: reduce_if_present "
              "halves failing_catalytic_converter after dedup"),
    # --- categorical guided outcomes ---------------------------------------
    dict(name="categorical_negative_no_leak_found",
         dtcs=[dtc("P0302")],
         observations=obs_outcome("coil_swap", "stays_on_cylinder"),
         flags=[],
         note="a 'no' answer is evidence too: coil_swap staying on the "
              "cylinder down-weights ignition_coil (x0.15)"),
    dict(name="categorical_no_leak",
         dtcs=[dtc("P0171")],
         observations=obs_outcome("inspect_intake", "no_leak_found"),
         flags=[],
         note="categorical outcome resolves via the outcomes map"),
    dict(name="evap_guided_categorical",
         dtcs=[dtc("P0442")],
         observations=obs_outcome("gas_cap_condition", "cap_damaged"),
         flags=[],
         note="P0442 family: damaged cap boosts loose_or_faulty_gas_cap"),
    # --- legacy yes/no fallback --------------------------------------------
    dict(name="legacy_yes_no_guided_fallback",
         dtcs=[dtc("P0171")],
         observations=obs_flat("inspect_intake", {"answer": 1.0}),
         flags=[],
         note="flat field map (no outcome key): falls back to "
              "updates_if_yes, recorded as outcome 'yes'"),
    dict(name="legacy_no_answer",
         dtcs=[dtc("P0171")],
         observations=obs_flat("inspect_intake", {"answer": 0.0}),
         flags=[],
         note="answer 0.0 with no outcome: no updates at all"),
    # --- code_context -------------------------------------------------------
    dict(name="code_context_p0171_p0174_boosts_vacuum_leak",
         dtcs=[dtc("P0171"), dtc("P0174")],
         observations={},
         flags=[],
         note="P0171+P0174: boost_if_present multiplies vacuum_leak by 1.8"),
    # --- multi-graph ranking ------------------------------------------------
    dict(name="multi_graph_p0302_p0171_ranked",
         dtcs=[dtc("P0302"), dtc("P0171")],
         observations=obs_flat("mode06_misfire_counts", {"dominant_share": 0.85}),
         flags=[],
         ranking=True,
         note="two graphs match; ranking is by best top score, then family"),
    dict(name="multi_graph_ranking_lean_catalyst",
         dtcs=[dtc("P0420"), dtc("P0171")],
         observations={},
         flags=[],
         ranking=True,
         note="no observations: ranking falls back to priors (+ code "
              "context reduce on the P0420 graph)"),
    # --- repair basket ------------------------------------------------------
    dict(name="basket_hose_satisfied_by_leak_found",
         dtcs=[dtc("P0171")],
         observations=obs_outcome("inspect_intake", "leak_found"),
         flags=[],
         basket="P0171_LEAN_BANK_1",
         note="confirming evidence present: replacement offered + "
              "diagnostic guidance"),
    dict(name="basket_hose_gated_without_visual_confirmation",
         dtcs=[dtc("P0171")],
         observations=obs_flat("fuel_trim_idle_vs_2500",
                               {"trim_idle": 12.0, "trim_2500": 2.0}),
         flags=[],
         basket="P0171_LEAN_BANK_1",
         note="score band reached but inspect_intake:leak_found never "
              "recorded: repair gated, diagnostic guidance still shown"),
    dict(name="basket_no_evidence",
         dtcs=[dtc("P0171")],
         observations={},
         flags=[],
         basket="P0171_LEAN_BANK_1",
         note="empty observations: basket refuses with 'not enough evidence'"),
    # --- safety stop --------------------------------------------------------
    dict(name="stop_if_flashing_mil_halts_scoring",
         dtcs=[dtc("P0171")],
         observations=obs_flat("fuel_trim_idle_vs_2500",
                               {"trim_idle": 12.0, "trim_2500": 2.0}),
         flags=["flashing_mil"],
         note="stop_if flag short-circuits scoring entirely"),
    # --- plain obd_live vectors ---------------------------------------------
    dict(name="obd_live_p0128_warmup",
         dtcs=[dtc("P0128")],
         observations=obs_flat("warmup_curve",
                               {"ect_after_10min_c": 60.0,
                                "ect_rise_c": 1.0, "ect_fluctuates": 0.0}),
         flags=[],
         note="single rule fires: thermostat_stuck_open boosted"),
    dict(name="obd_live_p0133_slow_o2",
         dtcs=[dtc("P0133")],
         observations=obs_flat("o2_response_time",
                               {"o2_switches_per_10s": 3.0,
                                "o2_amplitude_v": 0.5}),
         flags=[],
         note="slow switching: aging_o2_sensor boosted"),
]


def build_vector(spec):
    codes = [d["code"] for d in spec["dtcs"]]
    graphs = engine.graphs_for_dtcs(codes)
    assert graphs, f"no graph matched {codes}"
    flags = set(spec.get("flags", []))
    scored = engine.score_all(graphs, spec["observations"], flags,
                              dtc_evidence=spec["dtcs"])
    primary = scored[0]
    top = primary["top"]
    expected = {
        "family": primary["family"],
        "top": top["id"] if top else None,
        "confidence": primary["confidence"],
        "scores": {h["id"]: round(h["diagnostic_score"], 6)
                   for h in primary["hypotheses"]},
        "deduped_dropped": len(primary["evidence_deduped"]),
        "test_outcomes": primary["test_outcomes"],
        "code_context_applied": len(primary["code_context_applied"]),
    }
    vector = {
        "name": spec["name"],
        "note": spec.get("note", ""),
        "families": [g["family"] for g in graphs],
        "dtcs": spec["dtcs"],
        "observations": spec["observations"],
        "flags": spec.get("flags", []),
        "expect_stopped": bool(primary.get("stopped", False)),
        "expected": expected,
    }
    if spec.get("ranking"):
        vector["expected_ranking"] = [s["family"] for s in scored]
    if spec.get("basket"):
        fam = spec["basket"]
        entry = next(s for s in scored if s["family"] == fam)
        graph = next(g for g in graphs if g["family"] == fam)
        basket = engine.repair_basket(graph, entry, spec["observations"])
        vector["expected_basket"] = {
            "items": [i["repair_id"] for i in basket["items"]],
            "gated": [g_["repair_id"] for g_ in basket["gated"]],
            "guidance": [g_["repair_id"] for g_ in basket["guidance"]],
        }
    return vector


def main():
    vectors = [build_vector(v) for v in VECTORS]
    doc = {"version": 1,
           "generated_from": "backend/app/engine.py (canonical)",
           "vectors": vectors}

    golden_dir = ROOT / "tests" / "golden"
    golden_dir.mkdir(parents=True, exist_ok=True)
    main_path = golden_dir / "diagnostic_vectors.json"
    main_path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")

    res_dir = (ROOT / "android" / "core" / "src" / "test"
               / "resources" / "golden")
    res_dir.mkdir(parents=True, exist_ok=True)
    (res_dir / "diagnostic_vectors.json").write_text(
        main_path.read_text())
    graphs_dir = res_dir / "graphs"
    graphs_dir.mkdir(exist_ok=True)
    for f in GRAPH_FILES:
        (graphs_dir / f).write_text(
            (ROOT / "diagnostic-graphs" / f).read_text())

    print(f"wrote {main_path} ({len(vectors)} vectors)")
    for v in vectors:
        e = v["expected"]
        print(f"  {v['name']}: family={e['family']} top={e['top']} "
              f"conf={e['confidence']} dedup={e['deduped_dropped']} "
              f"ctx={e['code_context_applied']}")


if __name__ == "__main__":
    main()
