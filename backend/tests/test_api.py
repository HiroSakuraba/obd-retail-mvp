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
    return sid, r.json()["graph"]


def _new_p0171_session():
    r = client.post("/v1/sessions")
    assert r.status_code == 200
    sid = r.json()["session_id"]
    r = client.post(f"/v1/sessions/{sid}/vehicle",
                    json={"vin": "1FMCU0GD0JUA12345",
                          "dtcs": ["P0171"],
                          "pids": {"0C": 750.0, "05": 88.0}})
    assert r.status_code == 200
    assert r.json()["graph"] == "P0171_LEAN_BANK_1"
    return sid


def _new_p0171_session():
    sid, graph = _new_session(["P0171"])
    assert graph == "P0171_LEAN_BANK_1"
    return sid


def test_p0171_flow_vacuum_leak_wins():
    sid = _new_p0171_session()

    # Next test before any observation: the cheapest discriminating test.
    r = client.get(f"/v1/sessions/{sid}/next-test")
    assert r.json()["test"]["id"] == "fuel_trim_idle_vs_2500"

    # Observation: trims high at idle, much better at 2500 RPM.
    r = client.post(
        f"/v1/sessions/{sid}/observations",
        json={"test_id": "fuel_trim_idle_vs_2500",
              "fields": {"trim_idle": 33.0, "trim_2500": 12.0}})
    assert r.status_code == 200

    r = client.get(f"/v1/sessions/{sid}/diagnosis")
    d = r.json()
    assert not d["stopped"]
    top = d["top"]
    assert top["id"] == "vacuum_leak", top
    # priors .32*.2.2 etc, normalized: .704/1.264 = .557 -> moderate band
    assert abs(top["posterior"] - 0.557) < 0.01, top
    assert d["confidence"] == "moderate"

    r = client.get(f"/v1/sessions/{sid}/next-test")
    assert r.json()["test"]["id"] == "inspect_intake"

    # Repair basket: has a confidence field and a vacuum-leak repair.
    r = client.get(f"/v1/sessions/{sid}/repair-basket")
    b = r.json()
    assert "confidence" in b
    assert b["confidence"] == "moderate"
    assert any(i["hypothesis"] == "vacuum_leak" for i in b["items"]), b

    # Outcome recording.
    r = client.post(f"/v1/sessions/{sid}/outcome",
                    json={"repair_id": "hose-intake", "fixed": True})
    assert r.json()["recorded"] is True


def test_safety_flag_stops_diagnosis():
    sid = _new_p0171_session()
    r = client.post(
        f"/v1/sessions/{sid}/observations",
        json={"test_id": "fuel_trim_idle_vs_2500",
              "fields": {"trim_idle": 33.0, "trim_2500": 12.0},
              "flags": ["flashing_mil"]})
    assert r.status_code == 200
    r = client.get(f"/v1/sessions/{sid}/diagnosis")
    d = r.json()
    assert d["stopped"] is True
    assert "Stop driving" in d["stop_message"]


def test_insufficient_evidence_empty_basket():
    sid, _ = _new_session(["P0171"])
    r = client.get(f"/v1/sessions/{sid}/repair-basket")
    b = r.json()
    assert b["confidence"] == "insufficient"
    assert b["items"] == []


# ------------------------------------------------- schema conformance -------
def test_all_graphs_conform_to_schema():
    """Every diagnostic-graphs/*.json must satisfy the documented contract:
    required keys, priors summing to 1, top prior below the repair threshold,
    an `other` hypothesis, discriminating tests, and complete repairs."""
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
                assert t.get("updates_if_yes"), \
                    f"{family}/{t['id']}: guided test needs updates_if_yes"
        assert g["repairs"], f"{family}: needs at least one repair"
        for r in g["repairs"]:
            for k in ("hypothesis", "repair_id", "label", "parts",
                      "tools", "min_confidence"):
                assert k in r, f"{family}: repair missing {k}"
            assert r["hypothesis"] in ids, f"{family}: {r['repair_id']}"
            assert r["min_confidence"] in ("strong", "moderate"), f"{family}"
            assert r["parts"], f"{family}: {r['repair_id']} needs parts"
            for p in r["parts"]:
                assert {"sku", "qty", "required"} <= set(p), f"{family}"


# ------------------------------------------------- P0420 --------------------
def test_p0420_o2_ratio_points_at_cat_but_basket_waits():
    """The key discipline: a high downstream switching ratio raises the cat
    hypothesis but must NOT recommend a catalytic converter yet."""
    sid, graph = _new_session(["P0420"])
    assert graph == "P0420_CATALYST_EFFICIENCY_BANK1"

    # Basket is empty before any test runs.
    assert client.get(f"/v1/sessions/{sid}/repair-basket").json()["items"] == []

    r = client.post(
        f"/v1/sessions/{sid}/observations",
        json={"test_id": "o2_switching_ratio",
              "fields": {"switch_ratio": 0.85}})
    assert r.status_code == 200

    d = client.get(f"/v1/sessions/{sid}/diagnosis").json()
    top = d["top"]
    assert top["id"] == "failing_catalytic_converter", top
    assert abs(top["posterior"] - 0.657) < 0.01, top
    assert d["confidence"] == "moderate"

    # Cat repair requires strong confidence -> basket stays empty.
    b = client.get(f"/v1/sessions/{sid}/repair-basket").json()
    assert b["confidence"] == "moderate"
    assert b["items"] == [], b


def test_p0420_second_test_confirms_cat():
    sid, _ = _new_session(["P0420"])
    client.post(f"/v1/sessions/{sid}/observations",
                json={"test_id": "o2_switching_ratio",
                      "fields": {"switch_ratio": 0.85}})
    client.post(f"/v1/sessions/{sid}/observations",
                json={"test_id": "downstream_voltage_swing",
                      "fields": {"downstream_swing_v": 0.6}})
    d = client.get(f"/v1/sessions/{sid}/diagnosis").json()
    assert d["top"]["id"] == "failing_catalytic_converter"
    assert abs(d["top"]["posterior"] - 0.813) < 0.01
    assert d["confidence"] == "strong"
    b = client.get(f"/v1/sessions/{sid}/repair-basket").json()
    assert any(i["repair_id"] == "cat-replace-bank1" for i in b["items"]), b


def test_p0420_lazy_downstream_sensor_wins_instead():
    """The discriminator cuts the other way: a quiet downstream sensor
    implicates the sensor, not the converter."""
    sid, _ = _new_session(["P0420"])
    client.post(f"/v1/sessions/{sid}/observations",
                json={"test_id": "o2_switching_ratio",
                      "fields": {"switch_ratio": 0.2}})
    d = client.get(f"/v1/sessions/{sid}/diagnosis").json()
    assert d["top"]["id"] == "downstream_o2_sensor_fault", d["top"]
    assert d["confidence"] == "moderate"
    b = client.get(f"/v1/sessions/{sid}/repair-basket").json()
    assert any(i["repair_id"] == "downstream-o2-replace"
               for i in b["items"]), b


# ------------------------------------------------- P0442 --------------------
def test_p0442_gas_cap_inspection_wins():
    sid, graph = _new_session(["P0442"])
    assert graph == "P0442_EVAP_SMALL_LEAK"

    assert client.get(f"/v1/sessions/{sid}/repair-basket").json()["items"] == []

    r = client.post(
        f"/v1/sessions/{sid}/observations",
        json={"test_id": "gas_cap_condition",
              "fields": {"answer": 1.0}})
    assert r.status_code == 200

    d = client.get(f"/v1/sessions/{sid}/diagnosis").json()
    top = d["top"]
    assert top["id"] == "loose_or_faulty_gas_cap", top
    assert abs(top["posterior"] - 0.712) < 0.01, top
    assert d["confidence"] == "moderate"

    b = client.get(f"/v1/sessions/{sid}/repair-basket").json()
    assert any(i["repair_id"] == "gas-cap-replace" for i in b["items"]), b
    assert "drive cycles" in b["items"][0]["verification"], b


def test_p0442_safety_flag_stops():
    sid, _ = _new_session(["P0442"])
    client.post(f"/v1/sessions/{sid}/observations",
                json={"test_id": "gas_cap_condition",
                      "fields": {"answer": 1.0},
                      "flags": ["fuel_leak"]})
    d = client.get(f"/v1/sessions/{sid}/diagnosis").json()
    assert d["stopped"] is True
    assert "fire risk" in d["stop_message"]


# ------------------------------------------------- P0133 --------------------
def test_p0133_slow_switching_points_at_sensor():
    sid, graph = _new_session(["P0133"])
    assert graph == "P0133_O2_SLOW_RESPONSE_B1S1"

    assert client.get(f"/v1/sessions/{sid}/repair-basket").json()["items"] == []

    r = client.post(
        f"/v1/sessions/{sid}/observations",
        json={"test_id": "o2_response_time",
              "fields": {"o2_switches_per_10s": 3.0,
                         "o2_amplitude_v": 0.7}})
    assert r.status_code == 200

    d = client.get(f"/v1/sessions/{sid}/diagnosis").json()
    top = d["top"]
    assert top["id"] == "aging_o2_sensor", top
    assert abs(top["posterior"] - 0.593) < 0.01, top
    assert d["confidence"] == "moderate"

    b = client.get(f"/v1/sessions/{sid}/repair-basket").json()
    assert any(i["repair_id"] == "upstream-o2-replace"
               for i in b["items"]), b


def test_p0133_healthy_looking_sensor_admits_uncertainty():
    """A fast, full-amplitude sensor pushes the model toward `other` —
    it must not recommend a sensor it just measured as healthy."""
    sid, _ = _new_session(["P0133"])
    client.post(f"/v1/sessions/{sid}/observations",
                json={"test_id": "o2_response_time",
                      "fields": {"o2_switches_per_10s": 12.0,
                                 "o2_amplitude_v": 0.7}})
    d = client.get(f"/v1/sessions/{sid}/diagnosis").json()
    assert d["top"]["id"] == "other", d["top"]
    assert d["confidence"] == "insufficient"
    b = client.get(f"/v1/sessions/{sid}/repair-basket").json()
    assert b["items"] == [], b
