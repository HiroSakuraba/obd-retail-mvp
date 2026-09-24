"""Glovebox OBD retail diagnostic backend — minimal HTTP API (FastAPI).

Sessions are in-memory (MVP). Graphs are loaded from diagnostic-graphs/.
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from . import engine
from . import fitment

app = FastAPI(title="Glovebox OBD Diagnostic API", version="1.0.0")

SESSIONS: dict[str, dict] = {}


# ------------------------------------------------------------------ models ---
class DtcIn(BaseModel):
    """One trouble code with its OBD status. Plain strings are accepted
    too and default to confirmed."""
    code: str
    status: str = "confirmed"  # confirmed | pending | permanent


class VehicleIn(BaseModel):
    vin: str | None = None
    dtcs: list[str | DtcIn] = []
    pids: dict[str, float] = {}


class ObservationIn(BaseModel):
    test_id: str
    fields: dict[str, float] = {}
    outcome: str | None = None  # categorical outcome for guided tests
    flags: list[str] = []


class FreezeFrameIn(BaseModel):
    """Mode 02 freeze frame: the PID snapshot the ECU froze when the DTC
    set. First-class evidence, separate from live observations."""
    dtc: str
    pids: dict[str, float] = {}


class OutcomeIn(BaseModel):
    repair_id: str
    fixed: bool
    days_until_recurrence: int | None = None
    notes: str | None = None


class FitmentIn(BaseModel):
    repair_action: str
    vin: str | None = None
    year: int | None = None
    make: str | None = None
    model: str | None = None
    engine: str | None = None
    state: str | None = None  # e.g. "CA" — emissions compliance is fitment's job


def _session(sid: str) -> dict:
    s = SESSIONS.get(sid)
    if s is None:
        raise HTTPException(404, "unknown session")
    return s


def _normalize_dtcs(dtcs: list) -> list[dict]:
    """Accept plain code strings (=> confirmed) or {code, status} objects."""
    out = []
    for d in dtcs:
        if isinstance(d, str):
            out.append({"code": d, "status": "confirmed"})
        elif isinstance(d, DtcIn):
            st = d.status.lower()
            if st not in ("confirmed", "pending", "permanent"):
                raise HTTPException(422, f"unknown DTC status {d.status!r}")
            out.append({"code": d.code, "status": st})
        else:
            out.append({"code": d["code"],
                        "status": d.get("status", "confirmed").lower()})
    return out


def _codes(evidence: list[dict]) -> list[str]:
    return [e["code"] for e in evidence]


def _graphs(s: dict) -> list[dict]:
    gs = engine.graphs_for_dtcs(_codes(s["dtcs"]))
    if not gs:
        raise HTTPException(422, "no diagnostic graph for these DTCs")
    return gs


# ------------------------------------------------------------------ routes ---
@app.post("/v1/sessions")
def create_session() -> dict:
    sid = "sess_" + uuid.uuid4().hex[:8]
    SESSIONS[sid] = {"id": sid, "vin": None, "dtcs": [],
                     "observations": {}, "flags": set(), "outcome": None,
                     "freeze_frame": None}
    return {"session_id": sid}


@app.post("/v1/sessions/{sid}/vehicle")
def set_vehicle(sid: str, v: VehicleIn) -> dict:
    s = _session(sid)
    s["vin"] = v.vin
    s["dtcs"] = _normalize_dtcs(v.dtcs)
    s["pids"] = v.pids
    gs = engine.graphs_for_dtcs(_codes(s["dtcs"]))
    primary = gs[0] if gs else None
    return {"session_id": sid,
            "graphs": [g["family"] for g in gs],
            "primary_graph": primary["family"] if primary else None,
            "graph": primary["family"] if primary else None,
            "explanation": primary["explanation"] if primary else None,
            "dtc_evidence": s["dtcs"]}


@app.post("/v1/sessions/{sid}/observations")
def add_observation(sid: str, o: ObservationIn) -> dict:
    s = _session(sid)
    gs = _graphs(s)
    known = {t["id"] for g in gs for t in g["tests"]}
    if o.test_id not in known:
        raise HTTPException(422, f"unknown test {o.test_id}")
    s["observations"][o.test_id] = {"fields": o.fields,
                                    "outcome": o.outcome}
    s["flags"].update(o.flags)
    return {"session_id": sid, "recorded": o.test_id}


@app.post("/v1/sessions/{sid}/freeze-frame")
def set_freeze_frame(sid: str, f: FreezeFrameIn) -> dict:
    s = _session(sid)
    s["freeze_frame"] = {"dtc": f.dtc, "pids": f.pids}
    return {"session_id": sid, "recorded": True}


@app.get("/v1/sessions/{sid}/required-signals")
def get_required_signals(sid: str) -> dict:
    """The measurement contract for the app's OBD layer: every recipe the
    matched graphs need, how to acquire it, and whether it is done."""
    s = _session(sid)
    gs = _graphs(s)
    return {"session_id": sid,
            "signals": engine.required_signals(gs, s["observations"])}


@app.get("/v1/sessions/{sid}/next-test")
def get_next_test(sid: str) -> dict:
    s = _session(sid)
    gs = _graphs(s)
    t = engine.next_test(gs, s["observations"])
    if t is None:
        return {"session_id": sid, "test": None,
                "message": "No further tests in these graphs."}
    fam = next(g["family"] for g in gs
               if t["id"] in {x["id"] for x in g["tests"]})
    return {"session_id": sid, "family": fam, "test": t}


@app.get("/v1/sessions/{sid}/diagnosis")
def get_diagnosis(sid: str) -> dict:
    s = _session(sid)
    gs = _graphs(s)
    diagnoses = engine.score_all(gs, s["observations"], s["flags"],
                                 dtc_evidence=s["dtcs"])
    return {"session_id": sid,
            "diagnoses": diagnoses,
            "primary_family": diagnoses[0]["family"] if diagnoses else None,
            "freeze_frame": s.get("freeze_frame")}


@app.get("/v1/sessions/{sid}/repair-basket")
def get_repair_basket(sid: str) -> dict:
    s = _session(sid)
    gs = _graphs(s)
    primary = gs[0]
    scored = engine.score(primary, s["observations"], s["flags"],
                           dtc_evidence=s["dtcs"])
    return {"session_id": sid, "family": primary["family"],
            **engine.repair_basket(primary, scored, s["observations"])}


@app.post("/v1/sessions/{sid}/outcome")
def post_outcome(sid: str, o: OutcomeIn) -> dict:
    s = _session(sid)
    s["outcome"] = {"repair_id": o.repair_id, "fixed": o.fixed,
                    "days_until_recurrence": o.days_until_recurrence,
                    "notes": o.notes}
    return {"session_id": sid, "recorded": True}


@app.post("/v1/fitment/resolve")
def resolve_fitment_endpoint(f: FitmentIn) -> dict:
    """Diagnosis outputs a repair ACTION; this resolves it to a buyable,
    VIN-specific part. The two layers stay separate on purpose."""
    diagnostic_actions = {r["action"] for g in engine.GRAPHS.values()
                          for r in g["repairs"]
                          if r["recommendation_class"] == "diagnostic"}
    if f.repair_action in diagnostic_actions:
        return {"repair_action": f.repair_action,
                "parts_required": False,
                "note": "Diagnostic step (inspect/test) — no parts to "
                        "resolve. The basket guidance covers it."}
    try:
        return fitment.resolve_fitment(
            f.repair_action, vin=f.vin, year=f.year, make=f.make,
            model=f.model, engine=f.engine, state=f.state)
    except fitment.UnknownRepairAction:
        raise HTTPException(422, f"unknown repair action {f.repair_action!r}")
    except fitment.NoFitment:
        raise HTTPException(404, "no fitment for this vehicle")


@app.get("/v1/analytics/demand")
def analytics_demand(repair_family: str | None = None) -> dict:
    """Aggregated repair counts. Families with fewer than 5 sessions are
    suppressed (minimum privacy count).

    The count is ALL-TIME: there is deliberately no 7-day window here.
    The field is named accordingly until a regional/time-series demand
    system with real timestamps exists."""
    counts: dict[str, int] = {}
    for s in SESSIONS.values():
        g = engine.graph_for_dtcs(_codes(s["dtcs"]))
        if g is None:
            continue
        key = repair_family or g["family"]
        if repair_family and g["family"] != repair_family:
            continue
        counts[key] = counts.get(key, 0) + 1
    records = [
        {"repair_family": k, "sessions_all_time": n,
         "minimum_privacy_count_met": n >= 5}
        for k, n in sorted(counts.items())
    ]
    return {"records": [r for r in records if r["minimum_privacy_count_met"]],
            "suppressed": sum(1 for r in records
                              if not r["minimum_privacy_count_met"])}
