"""End-to-end API tests: scripted sessions walk scan -> test -> repair basket."""
from fastapi.testclient import TestClient

from app import engine
from app.main import app

client = TestClient(app)


def _new_session(dtcs, vin="1FMCU0GD0JUA12345"):
    r = client.post("/v1/sessions")
    assert r.status_code == 200
    sid = r.json()["session_id"]
    r = client.post(f"/v1/sessions/{sid}/vehicle",
                    json={"vin": vin, "dtcs": dtcs, "pids": {}})
    assert r.status_code == 200
    return sid, r.json()["primary_graph"]


def _new_p0171_session():
    sid, graph = _new_session(["P0171"])
    assert graph == "P0171_LEAN_BANK_1"
    return sid


def _observe(sid, test_id, fields=None, outcome=None, flags=None):
    r = client.post(
        f"/v1/sessions/{sid}/observations",
        json={"test_id": test_id, "fields": fields or {},
              "outcome": outcome, "flags": flags or []})
    assert r.status_code == 200, r.text
    return r


def _diagnosis(sid, family=None):
    d = client.get(f"/v1/sessions/{sid}/diagnosis").json()
    assert d["diagnoses"], d
    if family:
        return next(x for x in d["diagnoses"] if x["family"] == family)
    return d["diagnoses"][0]


# ------------------------------------------------- P0171 --------------------
def test_p0171_flow_vacuum_leak_wins():
    sid = _new_p0171_session()

    r = client.get(f"/v1/sessions/{sid}/next-test")
    assert r.json()["test"]["id"] == "fuel_trim_idle_vs_2500"

    _observe(sid, "fuel_trim_idle_vs_2500",
             {"trim_idle": 33.0, "trim_2500": 12.0})

    d = _diagnosis(sid)
    assert not d["stopped"]
    top = d["top"]
    assert top["id"] == "vacuum_leak", top
    # priors .32*2.2 etc, normalized: .704/1.264 = .557 -> moderate band
    assert abs(top["diagnostic_score"] - 0.557) < 0.01, top
    assert d["confidence"] == "moderate"

    r = client.get(f"/v1/sessions/{sid}/next-test")
    assert r.json()["test"]["id"] == "inspect_intake"

    r = client.post(f"/v1/sessions/{sid}/outcome",
                    json={"repair_id": "replace-intake-pcv-hose",
                          "fixed": True})
    assert r.json()["recorded"] is True


def test_p0171_trim_alone_does_not_sell_the_hose():
    """The reviewer's core complaint: a score alone must never sell a part.
    The first trim reading leads vacuum_leak at moderate confidence, but the
    hose stays gated until the visual inspection confirms the leak. The user
    gets guidance ('inspect the intake next'), not a buy button."""
    sid = _new_p0171_session()
    _observe(sid, "fuel_trim_idle_vs_2500",
             {"trim_idle": 33.0, "trim_2500": 12.0})

    b = client.get(f"/v1/sessions/{sid}/repair-basket").json()
    assert b["confidence"] == "moderate"
    # No part offered...
    assert b["items"] == [], b
    # ...but the hose is reported as gated (UI can say why), and the
    # diagnostic-class guidance tells the user what to do next.
    assert any(x["repair_id"] == "replace-intake-pcv-hose"
               for x in b["gated"]), b
    assert any(g["repair_id"] == "inspect-intake-system"
               for g in b["guidance"]), b
    assert "No part is recommended" in b["message"]

    # Visual confirmation unlocks the part: vacuum_leak goes strong and the
    # hose is offered with its fitment action (not a SKU — that's fitment's job).
    _observe(sid, "inspect_intake", outcome="leak_found")
    d = _diagnosis(sid)
    assert d["top"]["id"] == "vacuum_leak"
    assert d["confidence"] == "strong"
    b = client.get(f"/v1/sessions/{sid}/repair-basket").json()
    hose = next(i for i in b["items"]
                if i["repair_id"] == "replace-intake-pcv-hose")
    assert hose["action"] == "replace_intake_pcv_hose"
    assert hose["satisfied_evidence"] == ["inspect_intake:leak_found"]
    assert "sku" not in hose, "graphs must not name SKUs"


def test_p0171_no_leak_found_backs_off():
    """A 'no' answer is evidence too: a clean intake inspection pulls
    vacuum_leak back down instead of producing nothing."""
    sid = _new_p0171_session()
    _observe(sid, "fuel_trim_idle_vs_2500",
             {"trim_idle": 33.0, "trim_2500": 12.0})
    _observe(sid, "inspect_intake", outcome="no_leak_found")
    d = _diagnosis(sid)
    assert d["test_outcomes"]["inspect_intake"] == "no_leak_found"
    # 0.32*2.2*0.35 = 0.2464; total 0.8064 -> 0.306: back below the band.
    assert d["top"]["id"] == "vacuum_leak"
    assert d["confidence"] == "insufficient", d["top"]
    b = client.get(f"/v1/sessions/{sid}/repair-basket").json()
    assert b["items"] == [], b


def test_safety_flag_stops_diagnosis():
    sid = _new_p0171_session()
    _observe(sid, "fuel_trim_idle_vs_2500",
             {"trim_idle": 33.0, "trim_2500": 12.0},
             flags=["flashing_mil"])
    d = _diagnosis(sid)
    assert d["stopped"] is True
    assert "Stop driving" in d["stop_message"]


def test_insufficient_evidence_empty_basket():
    sid, _ = _new_session(["P0171"])
    b = client.get(f"/v1/sessions/{sid}/repair-basket").json()
    assert b["confidence"] == "insufficient"
    assert b["items"] == [] and b["guidance"] == []


# ------------------------------------------------- schema conformance -------
def test_all_graphs_conform_to_schema():
    """Every diagnostic-graphs/*.json must satisfy the documented contract:
    required keys, priors summing to 1, top prior below the repair threshold,
    an `other` hypothesis, discriminating tests with measurement recipes,
    categorical outcomes on guided tests, and repairs expressed as
    VIN-agnostic actions (no SKUs)."""
    required_top = {"family", "title", "explanation", "dtcs",
                    "initial_hypotheses", "tests", "safety", "repairs"}
    assert engine.GRAPHS, "no graphs loaded"
    for family, g in engine.GRAPHS.items():
        missing = required_top - set(g)
        assert not missing, f"{family}: missing keys {missing}"
        assert isinstance(g["dtcs"], list) and g["dtcs"], f"{family}: dtcs"
        priors = [h["prior"] for h in g["initial_hypotheses"]]
        assert abs(sum(priors) - 1.0) < 1e-9, f"{family}: priors sum"
        assert max(priors) < 0.45, f"{family}: top prior must stay < 0.45"
        ids = {h["id"] for h in g["initial_hypotheses"]}
        assert "other" in ids, f"{family}: needs an `other` hypothesis"
        assert isinstance(g["safety"].get("stop_if"), list), f"{family}: safety"
        for t in g["tests"]:
            assert t.get("id") and t.get("kind") in (
                "obd_live", "guided_visual", "guided_manual"), f"{family}: {t}"
            if t["kind"] == "obd_live":
                rules = t.get("result_rules", [])
                assert rules, f"{family}/{t['id']}: needs result_rules"
                for rule in rules:
                    assert rule.get("when") and rule.get("updates"), \
                        f"{family}/{t['id']}: rule needs when+updates"
                    for m in rule["updates"].values():
                        assert m > 0, f"{family}/{t['id']}: multiplier > 0"
            else:
                assert t.get("outcomes") or t.get("updates_if_yes"), \
                    f"{family}/{t['id']}: guided test needs outcomes"
        assert g["repairs"], f"{family}: needs at least one repair"
        for r in g["repairs"]:
            for k in ("hypothesis", "repair_id", "label", "action",
                      "component", "recommendation_class", "min_confidence"):
                assert k in r, f"{family}: repair missing {k}"
            assert r["hypothesis"] in ids, f"{family}: {r['repair_id']}"
            assert r["min_confidence"] in ("strong", "moderate"), f"{family}"
            assert r["recommendation_class"] in ("diagnostic", "replacement")
            assert "sku" not in str(r), f"{family}: SKU leaked into graph"


# ------------------------------------------------- P0420 --------------------
def test_p0420_o2_trace_points_at_cat_but_basket_waits():
    """The key discipline: one 60 s cruise trace raises the cat hypothesis
    to moderate, but the converter is expensive — the basket stays empty
    until a second, independent check (no exhaust leak) confirms it."""
    sid, graph = _new_session(["P0420"])
    assert graph == "P0420_CATALYST_EFFICIENCY_BANK1"

    assert client.get(f"/v1/sessions/{sid}/repair-basket").json()["items"] == []

    _observe(sid, "cruise_o2_trace",
             {"switch_ratio": 0.85, "downstream_swing_v": 0.6})

    d = _diagnosis(sid)
    top = d["top"]
    assert top["id"] == "failing_catalytic_converter", top
    # Single measurement: only the stronger of the two O2 rules counts.
    assert abs(top["diagnostic_score"] - 0.667) < 0.01, top
    assert d["confidence"] == "moderate"
    assert d["evidence_deduped"], "the weaker O2 rule must be deduped"

    # Cat repair requires strong confidence -> basket stays empty.
    b = client.get(f"/v1/sessions/{sid}/repair-basket").json()
    assert b["confidence"] == "moderate"
    assert b["items"] == [], b


def test_p0420_leak_check_confirms_cat():
    sid, _ = _new_session(["P0420"])
    _observe(sid, "cruise_o2_trace",
             {"switch_ratio": 0.85, "downstream_swing_v": 0.6})
    _observe(sid, "exhaust_leak_check", outcome="no_leak_found")
    d = _diagnosis(sid)
    assert d["top"]["id"] == "failing_catalytic_converter"
    assert abs(d["top"]["diagnostic_score"] - 0.771) < 0.01
    assert d["confidence"] == "strong"
    b = client.get(f"/v1/sessions/{sid}/repair-basket").json()
    cat = next(i for i in b["items"]
               if i["repair_id"] == "replace-catalytic-converter-bank1")
    assert cat["action"] == "replace_catalytic_converter_bank1"


def test_p0420_lazy_downstream_sensor_wins_instead():
    """The discriminator cuts the other way: a quiet downstream sensor
    implicates the sensor, not the converter."""
    sid, _ = _new_session(["P0420"])
    _observe(sid, "cruise_o2_trace", {"switch_ratio": 0.2})
    d = _diagnosis(sid)
    assert d["top"]["id"] == "downstream_o2_sensor_fault", d["top"]
    assert d["confidence"] == "moderate"
    b = client.get(f"/v1/sessions/{sid}/repair-basket").json()
    assert any(i["repair_id"] == "replace-downstream-o2-sensor"
               for i in b["items"]), b


# ------------------------------------------------- P030x --------------------
def test_p030x_coil_swap_outcomes():
    """Categorical outcomes: the misfire following the coil is decisive
    FOR the coil; staying on the cylinder is decisive AGAINST it."""
    sid, _ = _new_session(["P0302"])

    _observe(sid, "coil_swap", outcome="follows_coil")
    d = _diagnosis(sid, "P030X_MISFIRE")
    assert d["top"]["id"] == "ignition_coil", d["top"]
    assert abs(d["top"]["diagnostic_score"] - 0.632) < 0.01, d["top"]
    b = client.get(f"/v1/sessions/{sid}/repair-basket").json()
    assert any(i["repair_id"] == "replace-ignition-coil"
               for i in b["items"]), b

    # ...and the coil must NOT be sold before the swap test runs.
    sid2, _ = _new_session(["P0302"])
    b2 = client.get(f"/v1/sessions/{sid2}/repair-basket").json()
    assert b2["items"] == []


def test_p030x_misfire_stays_downweights_coil():
    sid, _ = _new_session(["P0302"])
    _observe(sid, "coil_swap", outcome="stays_on_cylinder")
    d = _diagnosis(sid, "P030X_MISFIRE")
    coil = next(h for h in d["hypotheses"] if h["id"] == "ignition_coil")
    assert coil["diagnostic_score"] < 0.1, coil
    assert d["top"]["id"] != "ignition_coil", d["top"]
    b = client.get(f"/v1/sessions/{sid}/repair-basket").json()
    assert all(i["repair_id"] != "replace-ignition-coil" for i in b["items"])


# ------------------------------------------------- P0442 --------------------
def test_p0442_gas_cap_inspection_wins():
    sid, graph = _new_session(["P0442"])
    assert graph == "P0442_EVAP_SMALL_LEAK"

    assert client.get(f"/v1/sessions/{sid}/repair-basket").json()["items"] == []

    _observe(sid, "gas_cap_condition", outcome="cap_damaged")

    d = _diagnosis(sid)
    top = d["top"]
    assert top["id"] == "loose_or_faulty_gas_cap", top
    assert abs(top["diagnostic_score"] - 0.712) < 0.01, top
    assert d["confidence"] == "moderate"

    b = client.get(f"/v1/sessions/{sid}/repair-basket").json()
    cap = next(i for i in b["items"] if i["repair_id"] == "replace-gas-cap")
    assert cap["action"] == "replace_gas_cap"
    assert "drive cycles" in cap["verification"], cap


def test_p0442_safety_flag_stops():
    sid, _ = _new_session(["P0442"])
    _observe(sid, "gas_cap_condition", outcome="cap_damaged",
             flags=["fuel_leak"])
    d = _diagnosis(sid)
    assert d["stopped"] is True
    assert "fire risk" in d["stop_message"]


# ------------------------------------------------- P0133 --------------------
def test_p0133_slow_switching_points_at_sensor():
    sid, graph = _new_session(["P0133"])
    assert graph == "P0133_O2_SLOW_RESPONSE_B1S1"

    assert client.get(f"/v1/sessions/{sid}/repair-basket").json()["items"] == []

    _observe(sid, "o2_response_time",
             {"o2_switches_per_10s": 3.0, "o2_amplitude_v": 0.7})

    d = _diagnosis(sid)
    top = d["top"]
    assert top["id"] == "aging_o2_sensor", top
    assert abs(top["diagnostic_score"] - 0.593) < 0.01, top
    assert d["confidence"] == "moderate"

    b = client.get(f"/v1/sessions/{sid}/repair-basket").json()
    assert any(i["repair_id"] == "replace-upstream-o2-sensor"
               for i in b["items"]), b


def test_p0133_healthy_looking_sensor_admits_uncertainty():
    """A fast, full-amplitude sensor pushes the model toward `other` —
    it must not recommend a sensor it just measured as healthy."""
    sid, _ = _new_session(["P0133"])
    _observe(sid, "o2_response_time",
             {"o2_switches_per_10s": 12.0, "o2_amplitude_v": 0.7})
    d = _diagnosis(sid)
    assert d["top"]["id"] == "other", d["top"]
    assert d["confidence"] == "insufficient"
    b = client.get(f"/v1/sessions/{sid}/repair-basket").json()
    assert b["items"] == [], b


# ------------------------------------------------- multi-DTC ----------------
def test_p0171_p0174_boosts_vacuum_leak():
    """P0171+P0174 is different evidence from P0171 alone: lean on both
    banks boosts the common-cause (vacuum leak) hypothesis via code_context."""
    sid, _ = _new_session(["P0171", "P0174"])
    d = _diagnosis(sid, "P0171_LEAN_BANK_1")
    ctx = d["code_context_applied"]
    assert any(c["dtc"] == "P0174" and c["hypothesis"] == "vacuum_leak"
               for c in ctx), ctx
    vac = next(h for h in d["hypotheses"] if h["id"] == "vacuum_leak")
    # 0.32*1.8=0.576 / 1.256 = 0.459
    assert abs(vac["diagnostic_score"] - 0.459) < 0.01, vac


def test_code_cluster_scores_every_graph():
    """P0302+P0171 keeps BOTH families alive and ranked instead of
    selecting one graph and discarding the other."""
    sid, _ = _new_session(["P0302", "P0171"])
    d = client.get(f"/v1/sessions/{sid}/diagnosis").json()
    fams = [x["family"] for x in d["diagnoses"]]
    assert "P030X_MISFIRE" in fams and "P0171_LEAN_BANK_1" in fams, fams
    assert d["primary_family"] == d["diagnoses"][0]["family"]
    mis = next(x for x in d["diagnoses"] if x["family"] == "P030X_MISFIRE")
    assert any(c["dtc"] == "P0171" for c in mis["code_context_applied"]), mis


def test_p0420_p0171_reduces_catalyst():
    """A concurrent lean code reduces the catalyst hypothesis: fix the
    lean fault before condemning the converter."""
    sid, _ = _new_session(["P0420", "P0171"])
    d = _diagnosis(sid, "P0420_CATALYST_EFFICIENCY_BANK1")
    ctx = d["code_context_applied"]
    assert any(c["dtc"] == "P0171"
               and c["hypothesis"] == "failing_catalytic_converter"
               for c in ctx), ctx
    cat = next(h for h in d["hypotheses"]
               if h["id"] == "failing_catalytic_converter")
    # 0.38*0.6=0.228 / 0.848 = 0.269
    assert abs(cat["diagnostic_score"] - 0.269) < 0.01, cat


# ------------------------------------------------- signals / fitment -------
def test_required_signals_contract():
    """The app-facing measurement contract: recipes, acquisition params,
    and acquisition state."""
    sid = _new_p0171_session()
    r = client.get(f"/v1/sessions/{sid}/required-signals")
    assert r.status_code == 200
    sigs = r.json()["signals"]
    trim = next(s for s in sigs if s["signal"] == "fuel_trim_idle_vs_2500")
    assert trim["service"] == "01"
    assert set(trim["pids"]) == {"06", "07", "0C"}
    assert trim["derived"] == ["trim_idle", "trim_2500"]
    assert trim["status"] == "supported"
    assert trim["acquired"] is False

    _observe(sid, "fuel_trim_idle_vs_2500",
             {"trim_idle": 33.0, "trim_2500": 12.0})
    sigs = client.get(f"/v1/sessions/{sid}/required-signals").json()["signals"]
    trim = next(s for s in sigs if s["signal"] == "fuel_trim_idle_vs_2500")
    assert trim["acquired"] is True


def test_freeze_frame_is_first_class():
    sid = _new_p0171_session()
    r = client.post(f"/v1/sessions/{sid}/freeze-frame",
                    json={"dtc": "P0171",
                          "pids": {"05": 88.0, "0C": 2120.0,
                                   "06": 9.0, "07": 21.0}})
    assert r.status_code == 200
    d = client.get(f"/v1/sessions/{sid}/diagnosis").json()
    assert d["freeze_frame"]["dtc"] == "P0171"
    assert d["freeze_frame"]["pids"]["0C"] == 2120.0


def test_fitment_resolves_action_to_sku():
    r = client.post("/v1/fitment/resolve",
                    json={"repair_action": "replace_catalytic_converter_bank1",
                          "vin": "2HKRW2H51FW123456"})
    assert r.status_code == 200, r.text
    f = r.json()
    assert f["sku"] == "CAT-CRV24-FED", f
    assert f["fitment_confirmed"] is True
    assert f["price_usd"] > 0 and f["guide_url"] and f["video_url"]
    assert "CARB" in (f["emissions_note"] or "")


def test_fitment_california_blocks_federal_cat():
    r = client.post("/v1/fitment/resolve",
                    json={"repair_action": "replace_catalytic_converter_bank1",
                          "year": 2014, "make": "Honda", "model": "CR-V",
                          "engine": "2.4L", "state": "CA"})
    f = r.json()
    assert f["stock"] == "not_available_ca", f
    assert "CALIFORNIA" in f["emissions_note"], f


def test_fitment_unknown_action_rejected():
    r = client.post("/v1/fitment/resolve",
                    json={"repair_action": "replace_flux_capacitor",
                          "vin": "1FMCU0GD0JUA12345"})
    assert r.status_code == 422, r.status_code


def test_fitment_diagnostic_action_has_no_parts():
    r = client.post("/v1/fitment/resolve",
                    json={"repair_action": "inspect_intake_for_leaks",
                          "vin": "1FMCU0GD0JUA12345"})
    assert r.status_code == 200, r.text
    assert r.json()["parts_required"] is False


# ---------------------------------------------------------------------------
# Regression tests: rule-expression `and`/`or` must not short-circuit the
# token stream. A host-language `or`/`and` that skips parsing the right side
# when the left side decides the result leaves tokens unconsumed, and the
# evaluator then throws "trailing tokens in rule".
# ---------------------------------------------------------------------------
def test_eval_rule_or_true_left_consumes_right():
    assert engine.eval_rule("x != 0 or y != 0", {"x": 1.0, "y": 0.0}) is True


def test_eval_rule_or_false_left():
    assert engine.eval_rule("x != 0 or y != 0", {"x": 0.0, "y": 1.0}) is True
    assert engine.eval_rule("x != 0 or y != 0", {"x": 0.0, "y": 0.0}) is False


def test_eval_rule_and_false_left_consumes_right():
    assert engine.eval_rule("x != 0 and y != 0", {"x": 0.0, "y": 1.0}) is False


def test_eval_rule_and_true_left():
    assert engine.eval_rule("x != 0 and y != 0", {"x": 1.0, "y": 1.0}) is True
    assert engine.eval_rule("x != 0 and y != 0", {"x": 1.0, "y": 0.0}) is False


def test_eval_rule_and_binds_tighter_than_or():
    # "and" binds tighter: x=0,y=1,z=0 -> 0 or (1 and 0) = False
    assert engine.eval_rule("x != 0 or y != 0 and z != 0",
                            {"x": 0.0, "y": 1.0, "z": 0.0}) is False
    assert engine.eval_rule("x != 0 or y != 0 and z != 0",
                            {"x": 0.0, "y": 1.0, "z": 1.0}) is True


def test_eval_rule_chained_or():
    assert engine.eval_rule("a != 0 or b != 0 or c != 0",
                            {"a": 0.0, "b": 0.0, "c": 1.0}) is True
    assert engine.eval_rule("a != 0 or b != 0 or c != 0",
                            {"a": 0.0, "b": 0.0, "c": 0.0}) is False


def test_eval_rule_or_with_not_and_parens():
    assert engine.eval_rule("x != 0 and not y != 0",
                            {"x": 1.0, "y": 0.0}) is True
    assert engine.eval_rule("(x != 0 or y != 0) and z != 0",
                            {"x": 1.0, "y": 0.0, "z": 1.0}) is True


# ---------------------------------------------------------------------------
# DTC status plumbing: confirmed/pending/permanent must survive the whole
# path session -> observations -> engine input instead of being collapsed
# into one string list (and permanent dropped entirely).
# ---------------------------------------------------------------------------
def test_dtc_status_preserved_end_to_end():
    r = client.post("/v1/sessions")
    sid = r.json()["session_id"]
    r = client.post(
        f"/v1/sessions/{sid}/vehicle",
        json={"vin": "1FMCU0GD0JUA12345",
              "dtcs": [{"code": "P0171", "status": "confirmed"},
                       {"code": "P0300", "status": "pending"},
                       {"code": "P0420", "status": "permanent"}],
              "pids": {}})
    assert r.status_code == 200
    body = r.json()
    assert body["dtc_evidence"] == [
        {"code": "P0171", "status": "confirmed"},
        {"code": "P0300", "status": "pending"},
        {"code": "P0420", "status": "permanent"}], body
    # graph selection still keys on codes, not status
    assert body["primary_graph"] == "P0171_LEAN_BANK_1", body

    d = client.get(f"/v1/sessions/{sid}/diagnosis").json()
    assert d["diagnoses"][0]["dtc_evidence"] == body["dtc_evidence"], d


def test_dtc_plain_strings_default_to_confirmed():
    sid, _ = _new_session(["P0171"])
    d = client.get(f"/v1/sessions/{sid}/diagnosis").json()
    assert d["diagnoses"][0]["dtc_evidence"] == [
        {"code": "P0171", "status": "confirmed"}], d


def test_dtc_unknown_status_rejected():
    r = client.post("/v1/sessions")
    sid = r.json()["session_id"]
    r = client.post(f"/v1/sessions/{sid}/vehicle",
                    json={"dtcs": [{"code": "P0171", "status": "maybe"}]})
    assert r.status_code == 422, r.status_code


def test_engine_score_carries_dtc_evidence():
    g = engine.graph_for_dtcs(["P0171"])
    ev = [{"code": "P0171", "status": "pending"}]
    out = engine.score(g, {}, set(), dtc_evidence=ev)
    assert out["dtc_evidence"] == ev, out
    stopped = engine.score(g, {}, {"flashing_mil"}, dtc_evidence=ev)
    assert stopped["stopped"] and stopped["dtc_evidence"] == ev, stopped
