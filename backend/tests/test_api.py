"""End-to-end API test: a P0171 session walks scan -> test -> repair basket."""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


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
    sid = _new_p0171_session()
    r = client.get(f"/v1/sessions/{sid}/repair-basket")
    b = r.json()
    assert b["confidence"] == "insufficient"
    assert b["items"] == []
