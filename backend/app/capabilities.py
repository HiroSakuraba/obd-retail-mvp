"""Honest transport capability declaration.

The diagnostic graphs declare *measurement recipes* (signals): how the app
acquires each derived field a rule reads. This module declares what the
transports can actually acquire. The checker
(``backend/tests/test_signal_capabilities.py``) fails the build if any graph
asks for more than the transports provide.

MUST mirror firmware/src/obd_allowlist.c by hand: the Mode-01 PID allowlist
plus the six allowlisted services. The checker pins the exact PID set, so
drift between this file and the firmware fails loudly.
"""
from __future__ import annotations

# Mode-01 PIDs the MVP graphs may read (two-char hex). Mirrors the firmware
# PID_ALLOWLIST exactly: 04 05 06 07 08 09 0B 0C 0D 0E 0F 10 11 14 15
# 1F 21 2F 33 42 43 44 45 46 47 4C. PIDs 14/15 (O2 voltages) were added for
# the P0133/P0420 recipes; everything else the graphs need was already there.
_ALLOWLISTED_PIDS = {
    "04", "05", "06", "07", "08", "09", "0B", "0C", "0D", "0E", "0F",
    "10", "11", "14", "15", "1F", "21", "2F", "33", "42", "43", "44",
    "45", "46", "47", "4C",
}

# (service, pid-or-None) pairs the transports can acquire. Service and PID
# are hex strings as they appear on the wire ("01", "14").
SUPPORTED: set[tuple[str, str | None]] = (
    {("01", pid) for pid in _ALLOWLISTED_PIDS}
    | {("02", pid) for pid in _ALLOWLISTED_PIDS}  # freeze frame
    | {("03", None), ("07", None), ("0A", None)}  # DTC services
    | {("09", "02")}                              # VIN
)

# Explicitly NOT implemented. Documented here so the gap is a conscious
# decision, not an oversight.
UNSUPPORTED: dict[tuple[str, str | None], str] = {
    ("06", None): "Mode 06 onboard monitoring results (e.g. per-cylinder "
                  "misfire counts) are not implemented on either transport. "
                  "Graphs may reference Mode 06 signals only as "
                  "status='planned', and no replacement repair may be gated "
                  "on one.",
}


def signal_acquirable(service: str, pids: list[str]) -> tuple[bool, str]:
    """Can the transport acquire this recipe's raw data today?"""
    service = service.upper()
    if (service, None) in UNSUPPORTED:
        return False, UNSUPPORTED[(service, None)]
    missing = [p for p in pids if (service, p.upper()) not in SUPPORTED]
    if missing:
        return False, (f"service {service} PID(s) {missing} are not on the "
                       f"transport allowlist")
    return True, "ok"


def check_graph(graph: dict) -> list[str]:
    """Return a list of capability violations for one graph (empty = clean)."""
    problems: list[str] = []
    fam = graph.get("family", "?")
    declared = {s["signal"]: s for s in graph.get("signals", [])}

    for t in graph.get("tests", []):
        tid = t["id"]
        for sig_id in t.get("signals", []):
            if sig_id not in declared:
                problems.append(
                    f"{fam}/{tid}: references undeclared signal {sig_id!r}")
        if t["kind"] == "obd_live" and not t.get("signals"):
            problems.append(
                f"{fam}/{tid}: obd_live test declares no measurement recipe")

    for sig_id, s in declared.items():
        ok, reason = signal_acquirable(s["service"], s.get("pids", []))
        if s.get("status") == "supported" and not ok:
            problems.append(
                f"{fam}: signal {sig_id!r} marked supported but not "
                f"acquirable: {reason}")
        if s.get("status") == "planned" and ok:
            problems.append(
                f"{fam}: signal {sig_id!r} marked planned but IS acquirable — "
                f"mark it supported")

    # A replacement repair must have at least one requires_any entry that
    # does not depend on a planned signal. Guided (human) tests are always
    # satisfiable; obd_live tests are satisfiable iff all their signals are
    # supported.
    planned_signals = {sid for sid, s in declared.items()
                       if s.get("status") == "planned"}
    test_by_id = {t["id"]: t for t in graph.get("tests", [])}

    def satisfiable_without_planned(req: str) -> bool:
        tid = req.split(":")[0]
        t = test_by_id.get(tid)
        if t is None:
            return False
        if t["kind"] != "obd_live":
            return True
        sigs = t.get("signals", [])
        return bool(sigs) and not (set(sigs) & planned_signals)

    for r in graph.get("repairs", []):
        rid = r.get("repair_id", "?")
        if r.get("recommendation_class") == "replacement":
            reqs = r.get("requires_any", [])
            if not reqs:
                problems.append(
                    f"{fam}: replacement repair {rid!r} has empty "
                    f"requires_any — a part must never be sold on priors alone")
            elif not any(satisfiable_without_planned(q) for q in reqs):
                problems.append(
                    f"{fam}: replacement repair {rid!r} can only be gated "
                    f"by planned signals {reqs} — no supported path exists")
        # A requires_any entry must name a real test.
        for q in r.get("requires_any", []):
            if q.split(":")[0] not in test_by_id:
                problems.append(
                    f"{fam}: repair {rid!r} requires unknown test {q!r}")
        # Outcomes referenced as test:outcome must exist.
        for q in r.get("requires_any", []):
            if ":" in q:
                tid, outcome = q.split(":", 1)
                t = test_by_id.get(tid)
                outs = (t.get("outcomes", {}) if t else {})
                legacy_yes = outcome == "yes" and t.get("updates_if_yes")
                if t is not None and outcome not in outs and not legacy_yes:
                    problems.append(
                        f"{fam}: repair {rid!r} requires unknown outcome "
                        f"{q!r}")
    return problems
