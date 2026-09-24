#!/usr/bin/env python3
"""obd_sim.py - stdlib-only simulated OBD dongle.

Speaks the JSON BLE protocol from the product doc over stdio: reads one
JSON request per line on stdin, writes one JSON response per line on stdout.

Usage:
    python3 obd_sim.py canned-vehicles/escape-2018-p0171.json < requests.txt
    python3 obd_sim.py --self-test   # scripted session with assertions

Only the allowlisted read ops are implemented; anything else gets
ERROR_FORBIDDEN_COMMAND, exactly like the firmware.
"""
import json
import sys

PROTO_VERSION = 1

# PIDs the simulated dongle will answer (mirrors the firmware allowlist).
PID_ALLOWLIST = {
    "04", "05", "06", "07", "08", "09", "0B", "0C", "0D", "0E", "0F", "10",
    "11", "1F", "21", "2F", "33", "42", "43", "44", "45", "46", "47", "4C",
}


def error_response(req_id, code):
    return {"v": PROTO_VERSION, "id": req_id or "unknown",
            "ok": False, "error": code}


def handle(vehicle, req):
    """One request dict -> one response dict."""
    if not isinstance(req, dict):
        return error_response(None, "ERROR_BAD_REQUEST")
    req_id = req.get("id")
    if req.get("v") != PROTO_VERSION:
        return error_response(req_id, "ERROR_VERSION")
    op = req.get("op")

    if op == "read_vin":
        data = {"vin": vehicle["vin"]}
    elif op == "read_dtcs_confirmed":
        data = {"dtcs": vehicle.get("dtcs_confirmed", [])}
    elif op == "read_dtcs_pending":
        data = {"dtcs": vehicle.get("dtcs_pending", [])}
    elif op == "read_dtcs_permanent":
        data = {"dtcs": vehicle.get("dtcs_permanent", [])}
    elif op == "read_pid":
        pid = str(req.get("pid", "")).upper()
        if pid not in PID_ALLOWLIST:
            return error_response(req_id, "ERROR_PID_NOT_ALLOWED")
        entry = vehicle.get("pids", {}).get(pid)
        if entry is None:
            return error_response(req_id, "ERROR_PID_NOT_SUPPORTED")
        data = {"pid": pid, "raw": entry["raw"],
                "value": entry["value"], "unit": entry["unit"]}
    elif op == "read_freeze_frame":
        data = vehicle.get("freeze_frame", {})
    else:
        return error_response(req_id, "ERROR_FORBIDDEN_COMMAND")
    return {"v": PROTO_VERSION, "id": req_id, "ok": True, "data": data}


def run_stdio(vehicle):
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            resp = error_response(None, "ERROR_BAD_REQUEST")
        else:
            resp = handle(vehicle, req)
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()


def self_test():
    import pathlib
    vehicle = json.loads(pathlib.Path(
        __file__).with_name("canned-vehicles")
        .joinpath("escape-2018-p0171.json").read_text())

    def q(req):
        return handle(vehicle, req)

    r = q({"v": 1, "id": "s1", "op": "read_vin"})
    assert r["ok"] and r["data"]["vin"] == "1FMCU0GD0JUA12345", r
    assert r["id"] == "s1"

    r = q({"v": 1, "id": "s2", "op": "read_dtcs_confirmed"})
    assert r["ok"] and r["data"]["dtcs"] == ["P0171"], r

    r = q({"v": 1, "id": "s3", "op": "read_dtcs_pending"})
    assert r["ok"] and r["data"]["dtcs"] == [], r

    r = q({"v": 1, "id": "s4", "op": "read_pid", "pid": "0C"})
    assert r["ok"] and r["data"]["value"] == 1726.0, r
    assert r["data"]["unit"] == "rpm"

    # Lean-bank evidence: STFT +11.2, LTFT +22.4 on bank 1.
    r = q({"v": 1, "id": "s5", "op": "read_pid", "pid": "07"})
    assert r["ok"] and abs(r["data"]["value"] - 22.4) < 1e-9, r

    # Non-allowlisted PID is refused, not answered.
    r = q({"v": 1, "id": "s6", "op": "read_pid", "pid": "FF"})
    assert not r["ok"] and r["error"] == "ERROR_PID_NOT_ALLOWED", r

    # Write-ish / unknown ops are forbidden.
    for op in ("raw_can_write", "clear_dtcs", "bogus_op"):
        r = q({"v": 1, "id": "s7", "op": op})
        assert not r["ok"] and r["error"] == "ERROR_FORBIDDEN_COMMAND", (op, r)

    # Version mismatch.
    r = q({"v": 2, "id": "s8", "op": "read_vin"})
    assert not r["ok"] and r["error"] == "ERROR_VERSION", r

    # Malformed JSON line handling via run path.
    r = handle(vehicle, "not a dict")
    assert not r["ok"] and r["error"] == "ERROR_BAD_REQUEST", r

    print("simulator self-test: 10 scripted checks passed")


def main(argv):
    if "--self-test" in argv:
        self_test()
        return 0
    paths = [a for a in argv[1:] if not a.startswith("-")]
    if not paths:
        print("usage: obd_sim.py <canned-vehicle.json> | --self-test",
              file=sys.stderr)
        return 2
    with open(paths[0]) as f:
        vehicle = json.load(f)
    run_stdio(vehicle)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
