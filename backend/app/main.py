"""Glovebox OBD retail diagnostic backend — minimal HTTP API (FastAPI).

Sessions are in-memory (MVP). Graphs are loaded from diagnostic-graphs/.
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from . import engine

app = FastAPI(title="Glovebox OBD Diagnostic API", version="1.0.0")

SESSIONS: dict[str, dict] = {}


# ------------------------------------------------------------------ models ---
class VehicleIn(BaseModel):
    vin: str | None = None
    dtcs: list[str] = []
    pids: dict[str, float] = {}


class ObservationIn(BaseModel):
    test_id: str
    fields: dict[str, float] = {}
    flags: list[str] = []


class OutcomeIn(BaseModel):
    repair_id: str
    fixed: bool
    days_until_recurrence: int | None = None
    notes: str | None = None


def _session(sid: str) -> dict:
    s = SESSIONS.get(sid)
    if s is None:
        raise HTTPException(404, "unknown session")
    return s


def _graph(s: dict) -> dict:
    g = engine.graph_for_dtcs(s["dtcs"])
    if g is None:
        raise HTTPException(422, "no diagnostic graph for these DTCs")
    return g


# ------------------------------------------------------------------ routes ---
@app.post("/v1/sessions")
def create_session() -> dict:
    sid = "sess_" + uuid.uuid4().hex[:8]
    SESSIONS[sid] = {"id": sid, "vin": None, "dtcs": [],
                     "observations": {}, "flags": set(), "outcome": None}
    return {"session_id": sid}


@app.post("/v1/sessions/{sid}/vehicle")
def set_vehicle(sid: str, v: VehicleIn) -> dict:
    s = _session(sid)
    s["vin"] = v.vin
    s["dtcs"] = v.dtcs
    s["pids"] = v.pids
    g = engine.graph_for_dtcs(v.dtcs)
    return {"session_id": sid, "graph": g["family"] if g else None,
            "explanation": g["explanation"] if g else None}


@app.post("/v1/sessions/{sid}/observations")
def add_observation(sid: str, o: ObservationIn) -> dict:
    s = _session(sid)
    g = _graph(s)
    if o.test_id not in {t["id"] for t in g["tests"]}:
        raise HTTPException(422, f"unknown test {o.test_id}")
    s["observations"][o.test_id] = o.fields
    s["flags"].update(o.flags)
    return {"session_id": sid, "recorded": o.test_id}


@app.get("/v1/sessions/{sid}/next-test")
def get_next_test(sid: str) -> dict:
    s = _session(sid)
    g = _graph(s)
    t = engine.next_test(g, s["observations"])
    if t is None:
        return {"session_id": sid, "test": None,
                "message": "No further tests in this graph."}
    return {"session_id": sid, "test": t}


@app.get("/v1/sessions/{sid}/diagnosis")
def get_diagnosis(sid: str) -> dict:
    s = _session(sid)
    g = _graph(s)
    return {"session_id": sid,
            **engine.score(g, s["observations"], s["flags"])}


@app.get("/v1/sessions/{sid}/repair-basket")
def get_repair_basket(sid: str) -> dict:
    s = _session(sid)
    g = _graph(s)
    scored = engine.score(g, s["observations"], s["flags"])
    return {"session_id": sid,
            **engine.repair_basket(g, scored, s["observations"])}


@app.post("/v1/sessions/{sid}/outcome")
def post_outcome(sid: str, o: OutcomeIn) -> dict:
    s = _session(sid)
    s["outcome"] = {"repair_id": o.repair_id, "fixed": o.fixed,
                    "days_until_recurrence": o.days_until_recurrence,
                    "notes": o.notes}
    return {"session_id": sid, "recorded": True}


@app.get("/v1/analytics/demand")
def analytics_demand(repair_family: str | None = None) -> dict:
    """Aggregated repair counts. Families with fewer than 5 sessions are
    suppressed (minimum privacy count)."""
    counts: dict[str, int] = {}
    for s in SESSIONS.values():
        g = engine.graph_for_dtcs(s["dtcs"])
        if g is None:
            continue
        key = repair_family or g["family"]
        if repair_family and g["family"] != repair_family:
            continue
        counts[key] = counts.get(key, 0) + 1
    records = [
        {"repair_family": k, "sessions_7d": n,
         "minimum_privacy_count_met": n >= 5}
        for k, n in sorted(counts.items())
    ]
    return {"records": [r for r in records if r["minimum_privacy_count_met"]],
            "suppressed": sum(1 for r in records
                              if not r["minimum_privacy_count_met"])}
