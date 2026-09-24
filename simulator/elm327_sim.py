#!/usr/bin/env python3
"""ELM327 AT-protocol vehicle simulator over TCP (stdlib only).

Speaks the subset of the ELM327 command set the app's read-only allowlist
uses: ATZ/ATE0/ATE1/ATL0/ATS0/ATH0/ATH1/ATSPn, then 0902 (VIN), 03/07/0A
(DTCs), 0100-0121 (PIDs), 0202 (freeze frame). Answers come from a canned
vehicle JSON (default: simulator/canned-vehicles/escape-2018-p0171.json).

Framing is realistic: the command is echoed back while echo is on (ATE1,
the power-on default), multi-frame VIN is emitted as "7E8 10 14 ..."-style
ISO-TP PCI frames when headers are on (ATH1) and as ELM327 "0: ..."-style
numbered lines when headers are off (ATH0), every response ends with the
">" prompt, and unsupported PIDs answer "NO DATA".

Usage:
    python3 simulator/elm327_sim.py [vehicle.json] [--port 35000]
    python3 simulator/elm327_sim.py --self-test   # full client session, asserts values
"""

import json
import os
import socket
import sys
import threading

DEFAULT_VEHICLE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "canned-vehicles",
    "escape-2018-p0171.json")

# ------------------------------------------------------------------ encode --

def encode_dtc(dtc):
    """'P0171' -> [0x01, 0x71] per SAE J2012."""
    head = {"P": 0, "C": 1, "B": 2, "U": 3}[dtc[0]]
    b1 = (head << 6) | (int(dtc[1]) << 4) | int(dtc[2], 16)
    return [b1, int(dtc[3:5], 16)]


def hexbytes(bs):
    return " ".join("%02X" % b for b in bs)


def vin_payload(vin):
    return [0x49, 0x02, 0x01] + [ord(c) for c in vin]


def vin_frames_numbered(vin):
    """ELM327 multi-line style used when headers are off (ATH0)."""
    data = vin_payload(vin)
    lines = ["014"]
    for i in range(0, len(data), 7):
        lines.append("%d: %s" % (i // 7, hexbytes(data[i:i + 7])))
    return lines


def vin_frames_pci(vin):
    """Raw ISO-TP PCI frames as ELM327 shows them with headers on (ATH1)."""
    data = vin_payload(vin)
    lines = ["7E8 10 %02X %s" % (len(data), hexbytes(data[:6]))]
    rest = data[6:]
    for n in range(1, (len(rest) + 6) // 7 + 1):
        lines.append("7E8 2%X %s" % (n, hexbytes(rest[(n - 1) * 7:n * 7])))
    return lines


def single_frame(service_bytes, headers):
    """Wrap payload bytes in a single ISO-TP frame, with or without CAN id."""
    body = hexbytes(service_bytes)
    if headers:
        return ["7E8 %02X %s" % (len(service_bytes), body)]
    return [body]


def freeze_frame_lines(vehicle, headers):
    ff = vehicle["freeze_frame"]
    out = [0x42, 0x02] + encode_dtc(ff["dtc"])
    enc = {"05": lambda v: [int(v) + 40],
           "0C": lambda v: [(int(v * 4) >> 8) & 0xFF, int(v * 4) & 0xFF],
           "0D": lambda v: [int(v)],
           "10": lambda v: [(int(v * 100) >> 8) & 0xFF, int(v * 100) & 0xFF]}
    for pid, val in ff["pids"].items():
        out += [int(pid, 16)] + enc[pid](val)
    return single_frame(out, headers)


# ------------------------------------------------------------------ server --

class ElmState:
    def __init__(self, vehicle):
        self.vehicle = vehicle
        self.echo = True     # ATE1 is the power-on default
        self.headers = False  # ATH0 default; the app's init sends ATH1

    def handle(self, cmd):
        c = cmd.strip().upper()
        v = self.vehicle
        if c == "ATZ":
            return ["ELM327 v2.1"]
        if c == "ATE0":
            self.echo = False
            return ["OK"]
        if c == "ATE1":
            self.echo = True
            return ["OK"]
        if c in ("ATL0", "ATL1", "ATS0", "ATS1"):
            return ["OK"]
        if c == "ATH0":
            self.headers = False
            return ["OK"]
        if c == "ATH1":
            self.headers = True
            return ["OK"]
        if c.startswith("ATSP"):
            return ["OK"]
        if c.startswith("AT"):
            return ["?"]
        if c == "0902":
            return vin_frames_pci(v["vin"]) if self.headers else vin_frames_numbered(v["vin"])
        if c in ("03", "07", "0A"):
            key = {"03": "dtcs_confirmed", "07": "dtcs_pending",
                   "0A": "dtcs_permanent"}[c]
            body = [0x40 + int(c, 16)]
            for dtc in v[key]:
                body += encode_dtc(dtc)
            if not v[key]:
                body += [0x00]
            return single_frame(body, self.headers)
        if c == "0202":
            return freeze_frame_lines(v, self.headers)
        if c.startswith("01") and len(c) == 4:
            pid = c[2:4]
            if pid == "00":
                return single_frame([0x41, 0x00, 0xBE, 0x1F, 0xA8, 0x13], self.headers)
            if pid == "20":
                return single_frame([0x41, 0x20, 0x80, 0x00, 0x00, 0x00], self.headers)
            entry = v.get("pids", {}).get(pid)
            if entry is None:
                return ["NO DATA"]
            raw = [int(t, 16) for t in entry["raw"].split()]
            return single_frame([0x41, int(pid, 16)] + raw, self.headers)
        return ["?"]


def handle_conn(conn, vehicle):
    state = ElmState(vehicle)
    buf = b""
    try:
        while True:
            data = conn.recv(1024)
            if not data:
                return
            buf += data
            while b"\r" in buf:
                line, buf = buf.split(b"\r", 1)
                cmd = line.decode("ascii", "replace").strip()
                if not cmd:
                    continue
                lines = state.handle(cmd)
                out = b""
                if state.echo:
                    out += cmd.encode("ascii", "replace") + b"\r"
                out += "\r".join(lines).encode("ascii") + b"\r\r>"
                conn.sendall(out)
    except (ConnectionResetError, BrokenPipeError):
        pass
    finally:
        conn.close()


def serve(port, vehicle, ready=None):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(8)
    if ready is not None:
        ready.set()
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=handle_conn, args=(conn, vehicle),
                         daemon=True).start()


# --------------------------------------------------------------- self-test --

class ElmClient:
    """Minimal blocking ELM327 client used only by the self-test."""

    def __init__(self, port):
        self.s = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.buf = b""

    def cmd(self, command):
        self.s.sendall(command.encode("ascii") + b"\r")
        while b">" not in self.buf:
            chunk = self.s.recv(4096)
            if not chunk:
                raise AssertionError("connection closed waiting for '>'")
            self.buf += chunk
        text, self.buf = self.buf.split(b">", 1)
        return text.decode("ascii", "replace")


def payload_lines(resp, command, echo):
    lines = [l for l in resp.replace("\r", "\n").split("\n") if l.strip()]
    if echo and lines and lines[0].strip() == command:
        lines = lines[1:]
    return [l for l in lines if l.strip() not in ("014",)]


def tokens_of(lines):
    toks = []
    for l in lines:
        l = l.split(":", 1)[-1]  # strip "N:" numbering
        parts = l.split()
        if len(parts) >= 2 and len(parts[0]) == 3 and parts[1][0] in "12":
            parts = parts[2:]  # strip CAN id + ISO-TP PCI
        toks += [p for p in parts if len(p) <= 2]
    return toks


def self_test(port, vehicle):
    ready = threading.Event()
    t = threading.Thread(target=serve, args=(port, vehicle, ready), daemon=True)
    t.start()
    if not ready.wait(timeout=10):
        raise AssertionError("sim server did not start listening")
    c = ElmClient(port)
    n = [0]

    def check(cond, label):
        assert cond, "FAILED: " + label
        n[0] += 1

    r = c.cmd("ATZ")
    check("ELM327" in r, "ATZ banner, got %r" % r)
    for at in ("ATE0", "ATL0", "ATS0", "ATH1", "ATSP0"):
        r = c.cmd(at)
        check("OK" in r, "%s -> OK, got %r" % (at, r))

    # VIN with headers on (PCI frames), then headers off (numbered lines).
    for headers in (True, False):
        r = c.cmd("ATH1" if headers else "ATH0")
        r = c.cmd("0902")
        toks = tokens_of(payload_lines(r, "0902", echo=False))
        i = toks.index("49")
        check(toks[i + 1] == "02", "49 02 header")
        vin = "".join(chr(int(x, 16)) for x in toks[i + 3:i + 20])
        check(vin == vehicle["vin"], "VIN %r" % vin)
    c.cmd("ATH1")

    def dtcs(cmd, key):
        r = c.cmd(cmd)
        toks = tokens_of(payload_lines(r, cmd, echo=False))
        i = toks.index("%02X" % (0x40 + int(cmd, 16)))
        out = []
        j = i + 1
        while j + 1 < len(toks):
            b1, b2 = int(toks[j], 16), int(toks[j + 1], 16)
            if b1 == 0 and b2 == 0:
                break
            sys = "PCBU"[b1 >> 6]
            out.append("%s%X%X%X%X" % (sys, (b1 >> 4) & 3, b1 & 15, b2 >> 4, b2 & 15))
            j += 2
        check(out == vehicle[key], "%s -> %r" % (cmd, out))

    dtcs("03", "dtcs_confirmed")
    dtcs("07", "dtcs_pending")
    dtcs("0A", "dtcs_permanent")

    def pid(pid_hex, formula):
        r = c.cmd("01" + pid_hex)
        toks = tokens_of(payload_lines(r, "01" + pid_hex, echo=False))
        i = toks.index("41")
        check(toks[i + 1] == pid_hex, "41 %s header" % pid_hex)
        data = [int(x, 16) for x in toks[i + 2:]]
        return formula(data)

    check(abs(pid("0C", lambda d: (d[0] * 256 + d[1]) / 4) - 1726.0) < 1e-9, "RPM")
    check(abs(pid("05", lambda d: d[0] - 40) - 88.0) < 1e-9, "coolant")
    check(abs(pid("06", lambda d: (d[0] - 128) * 100 / 128) - 11.2) < 0.5, "STFT B1")
    check(abs(pid("07", lambda d: (d[0] - 128) * 100 / 128) - 22.4) < 0.5, "LTFT B1")
    check(abs(pid("10", lambda d: (d[0] * 256 + d[1]) / 100) - 3.1) < 0.05, "MAF")
    check(abs(pid("04", lambda d: d[0] * 100 / 255) - 23.1) < 0.5, "load")
    check(abs(pid("11", lambda d: d[0] * 100 / 255) - 18.0) < 0.5, "throttle")
    check(abs(pid("1F", lambda d: d[0] * 256 + d[1]) - 300.0) < 1e-9, "runtime")
    check(abs(pid("21", lambda d: d[0] * 256 + d[1]) - 0.0) < 1e-9, "dist MIL")
    check(abs(pid("33", lambda d: d[0]) - 101.0) < 1e-9, "baro")
    check(abs(pid("0B", lambda d: d[0]) - 62.0) < 1e-9, "MAP")
    check(abs(pid("0D", lambda d: d[0]) - 0.0) < 1e-9, "speed")
    check(abs(pid("0E", lambda d: d[0] / 2 - 64) - 0.0) < 1e-9, "timing")

    r = c.cmd("0100")
    check("41 00" in r.replace("\r", " "), "0100 supported-PID bitmap")
    r = c.cmd("01FF")
    check("NO DATA" in r, "unsupported PID -> NO DATA, got %r" % r)
    r = c.cmd("0202")
    check("42 02 01 71" in r.replace("\r", " ").replace("  ", " "),
          "freeze frame 42 02 + P0171, got %r" % r)
    r = c.cmd("ATXYZ")
    check("?" in r, "unknown AT -> ?, got %r" % r)

    # Echo-on path: fresh connection defaults to ATE1.
    c2 = ElmClient(port)
    r = c2.cmd("0902")
    check(r.strip().startswith("0902"), "echo on by default")

    print("elm327 sim self-test: %d scripted checks passed" % n[0])


def main(argv):
    port = 35000
    vehicle_path = DEFAULT_VEHICLE
    rest = []
    for a in argv[1:]:
        if a == "--self-test":
            rest.append(a)
        elif a == "--port" :
            rest.append(a)
        elif a.isdigit() and rest and rest[-1] == "--port":
            port = int(a)
            rest.pop()
        elif a.endswith(".json"):
            vehicle_path = a
        else:
            print("unknown arg %r" % a, file=sys.stderr)
            return 2
    vehicle = json.load(open(vehicle_path))
    if "--self-test" in rest:
        self_test(port, vehicle)
    else:
        print("elm327 sim on 127.0.0.1:%d (%s)" % (port, vehicle_path))
        serve(port, vehicle)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
