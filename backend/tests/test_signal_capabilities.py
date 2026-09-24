"""Measurement-recipe capability checks.

The diagnostic graphs declare *how* each piece of evidence is acquired
(signals: service, PIDs, sample rate, conditions, derived fields). These
tests fail the build if any graph asks for data the transports cannot
produce — the exact gap the architecture review found (graphs asking for
Mode 06 counts, O2 traces, and trim protocols the scanner had no way to
run).
"""
import math
import re

from app import capabilities, engine

_KEYWORDS = {"and", "or", "not"}


def _rule_fields(expr: str) -> set[str]:
    toks = [t for t in engine._TOKEN.findall(expr) if t.strip()]
    return {t for t in toks
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", t)
            and t not in _KEYWORDS}


def test_no_capability_violations():
    problems = []
    for family, g in engine.GRAPHS.items():
        problems.extend(capabilities.check_graph(g))
    assert not problems, "\n".join(problems)


def test_every_rule_field_comes_from_a_declared_signal():
    """A when-rule may only read fields its test's measurement recipes
    produce. No more 'conceptual signals' with no acquisition path."""
    problems = []
    for family, g in engine.GRAPHS.items():
        declared = {s["signal"]: s for s in g.get("signals", [])}
        for t in g.get("tests", []):
            if t["kind"] != "obd_live":
                continue
            produced = set()
            for sig_id in t.get("signals", []):
                produced.update(declared.get(sig_id, {}).get("derived", []))
            for rule in t.get("result_rules", []):
                for f in _rule_fields(rule["when"]):
                    if f not in produced:
                        problems.append(
                            f"{family}/{t['id']}: rule reads {f!r}, which no "
                            f"declared signal produces")
    assert not problems, "\n".join(problems)


def test_mode06_is_planned_not_silent():
    """Mode 06 (misfire counts) is honestly marked planned: the p030x graph
    declares the gap instead of pretending the scanner can read it, and the
    decisive coil-swap test needs no signal at all."""
    g = engine.GRAPHS["P030X_MISFIRE"]
    sig = {s["signal"]: s for s in g["signals"]}["mode06_misfire_counts"]
    assert sig["status"] == "planned"
    assert sig["service"] == "06"
    ok, _ = capabilities.signal_acquirable(sig["service"], sig["pids"])
    assert not ok, "Mode 06 must stay honestly unacquirable"


def test_o2_pids_are_supported():
    """P0420/P0133 recipes need Mode-01 PIDs 14/15 (O2 voltages); both
    transports allowlist them (firmware + capability table agree)."""
    for family in ("P0420_CATALYST_EFFICIENCY_BANK1", "P0133_O2_SLOW_RESPONSE_B1S1"):
        g = engine.GRAPHS[family]
        for s in g["signals"]:
            assert s["status"] == "supported", f"{family}/{s['signal']}"
            ok, reason = capabilities.signal_acquirable(s["service"], s["pids"])
            assert ok, f"{family}/{s['signal']}: {reason}"


def test_capability_table_matches_firmware_allowlist():
    """The Python capability table must pin the exact firmware PID set so
    drift fails loudly here instead of silently on a car."""
    from pathlib import Path
    src = Path("firmware/src/obd_allowlist.c").read_text()
    fw_pids = set(re.findall(r'^\s*"([0-9A-F]{2})",', src, re.M))
    py_pids = {pid for (svc, pid) in capabilities.SUPPORTED if svc == "01"}
    assert fw_pids == py_pids, f"firmware={sorted(fw_pids)} python={sorted(py_pids)}"
    assert ("14" in py_pids) and ("15" in py_pids)


def test_repairs_are_actions_not_skus():
    """Graphs output repair actions; SKU/guide/video live in the fitment
    service. No vehicle-specific part data may leak back into a graph."""
    forbidden = {"parts", "sku", "guide_id", "video_id"}
    for family, g in engine.GRAPHS.items():
        for r in g["repairs"]:
            leaked = forbidden & set(r)
            assert not leaked, f"{family}/{r['repair_id']}: {leaked}"
            for k in ("action", "component", "recommendation_class"):
                assert r.get(k), f"{family}/{r['repair_id']}: missing {k}"
            assert r["recommendation_class"] in ("diagnostic", "replacement")


def test_no_posterior_in_engine_output():
    """Scores are uncalibrated expert weights; the engine must not call
    them posteriors."""
    g = engine.graph_for_dtcs(["P0171"])
    out = engine.score(g, {"fuel_trim_idle_vs_2500":
                           {"fields": {"trim_idle": 33.0, "trim_2500": 12.0}}},
                       set())
    assert "diagnostic_score" in out["top"]
    assert "posterior" not in out["top"]
    assert all("diagnostic_score" in h and "posterior" not in h
               for h in out["hypotheses"])


def test_evidence_group_dedup_p0420():
    """The two P0420 O2 rules come from ONE 60 s cruise trace. Both match
    here, but per hypothesis only the stronger multiplier may apply — the
    weaker is reported as deduped, never multiplied."""
    g = engine.GRAPHS["P0420_CATALYST_EFFICIENCY_BANK1"]
    out = engine.score(
        g, {"cruise_o2_trace": {"fields": {"switch_ratio": 0.85,
                                           "downstream_swing_v": 0.6}}},
        set())
    top = out["top"]
    assert top["id"] == "failing_catalytic_converter"
    # cat: max(2.6, 2.0) = 2.6 -> 0.38*2.6=0.988; total 1.482 -> 0.667.
    # Without dedup the swing rule's extra x2.0 would give 0.813.
    assert abs(top["diagnostic_score"] - 0.667) < 0.01, top
    assert out["confidence"] == "moderate"
    dropped = {(d["hypothesis"], d["dropped_multiplier"])
               for d in out["evidence_deduped"]}
    assert ("failing_catalytic_converter", 2.0) in dropped, dropped
    assert ("downstream_o2_sensor_fault", 0.7) in dropped, dropped
