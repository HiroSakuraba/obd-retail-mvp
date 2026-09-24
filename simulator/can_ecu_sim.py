#!/usr/bin/env python3
"""CAN-frame-level ECU simulator (stdlib only).

This is the bench rig for the ESP32 dongle firmware in firmware/esp32/.
Unlike elm327_sim.py (which speaks the ELM327 *AT text protocol* over TCP),
this simulator speaks raw CAN frames over TCP so the firmware's actual
CAN/ISO-TP path can be validated on the host before touching hardware.

Wire framing (both directions): 14 bytes per frame =
    struct ">I B B 8s"  ->  CAN id (32-bit), DLC (0..8), flags, data padded
    to 8. Flags bit 0 = extended (29-bit) identifier.

--variant 11 (default): functional requests on 0x7DF, answers from 0x7E8.
--variant 29: functional requests on 0x18DB33F1, answers from 0x18DAF110.
Bitrate is not simulated over TCP (the firmware's variant detector is
exercised through identifier matching, not timing).

    09 02        -> multi-frame VIN  (49 02 01 + 17 ASCII bytes)
    03 / 07 / 0A -> DTC lists       (43 / 47 / 4A + 2 bytes per code)
    01 00        -> Mode-01 support bitmap (41 00 ...)
    01 <pid>     -> live PID         (41 <pid> <raw bytes>) or NO RESPONSE
                     for unsupported PIDs -- exactly like a real ECU, which
                     stays silent; the firmware must time out and skip.
    02 <pid> 00  -> freeze frame     (42 <pid> 00 <raw bytes>) or NO RESPONSE.
                     The sim has no time machine: frozen values equal the
                     current ones. PID 02 returns the DTC that set the code.

ISO-TP: requests here are always single-frame. The VIN response is a First
Frame; the simulator waits for the client's Flow Control frame (on the
functional id or the physical request id) before sending Consecutive
Frames, then expects the sequence numbers 0x21, 0x22, ...

Answers come from a canned vehicle JSON (default: the P0171 Escape), the
same files elm327_sim.py uses.

Usage:
    python3 simulator/can_ecu_sim.py [--vehicle FILE] [--port 35001]
                                     [--variant 11|29]
                                     [--frame-log frames.jsonl]
    python3 simulator/can_ecu_sim.py --self-test   # protocol self-check

The optional --frame-log records every frame the *client* transmitted as
JSON lines {"id": "0x000007DF", "pci": "SF", "service": "0x01"}. The
firmware test suite uses it as a wire-level read-only audit: nothing but
allowlisted services and ISO-TP flow control may ever be transmitted.
"""

import json
import os
import socket
import struct
import sys
import threading

DEFAULT_VEHICLE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "canned-vehicles",
    "escape-2018-p0171.json")

# Wire framing: struct ">I B B 8s" = 32-bit CAN id, DLC, flags, 8 data
# bytes. Flags bit 0 = extended (29-bit) identifier. Matches
# firmware/esp32/src/tcp_transport.c.
FRAME = struct.Struct(">I B B 8s")
FLAG_EXTD = 0x01

# 11-bit variant ids; the 29-bit set is derived per variant.
FUNC_ID_11B = 0x7DF
RESP_ID_11B = 0x7E8
PHYS_ID_11B = 0x7E0  # physical request id paired with 0x7E8
FUNC_ID_29B = 0x18DB33F1
RESP_ID_29B = 0x18DAF110
PHYS_ID_29B = 0x18DA10F1


# ------------------------------------------------------------------ encode --

def encode_dtc(dtc):
    """'P0171' -> [0x01, 0x71] per SAE J2012."""
    head = {"P": 0, "C": 1, "B": 2, "U": 3}[dtc[0]]
    b1 = (head << 6) | (int(dtc[1]) << 4) | int(dtc[2], 16)
    return [b1, int(dtc[3:5], 16)]


def support_bitmap(pids):
    """Mode 01 PID 0x00 support bitmap for PIDs 0x01..0x20."""
    out = [0, 0, 0, 0]
    for pid in pids:
        if 1 <= pid <= 0x20:
            out[(pid - 1) // 8] |= 1 << (7 - ((pid - 1) % 8))
    return out


# ------------------------------------------------------------------- server --

class Ecu:
    def __init__(self, vehicle, frame_log=None, variant=11):
        self.vin = vehicle["vin"]
        self.dtcs = {
            0x03: vehicle.get("dtcs_confirmed", []),
            0x07: vehicle.get("dtcs_pending", []),
            0x0A: vehicle.get("dtcs_permanent", []),
        }
        self.pids = {}
        for k, v in vehicle.get("pids", {}).items():
            raw = bytes(int(t, 16) for t in v["raw"].split())
            self.pids[int(k, 16)] = raw
        if variant == 29:
            self.func_id, self.resp_id, self.phys_id = \
                FUNC_ID_29B, RESP_ID_29B, PHYS_ID_29B
        else:
            self.func_id, self.resp_id, self.phys_id = \
                FUNC_ID_11B, RESP_ID_11B, PHYS_ID_11B
        self._extd = (variant == 29)
        self._log = frame_log
        self._lock = threading.Lock()

    # -- low-level io -------------------------------------------------
    def _send(self, conn, can_id, data):
        data = bytes(data)
        conn.sendall(FRAME.pack(can_id, len(data),
                                FLAG_EXTD if self._extd else 0,
                                data.ljust(8, b"\x00")))

    @staticmethod
    def _recv_frame(conn):
        buf = b""
        while len(buf) < FRAME.size:
            chunk = conn.recv(FRAME.size - len(buf))
            if not chunk:
                return None
            buf += chunk
        can_id, dlc, flags, data = FRAME.unpack(buf)
        return can_id, (flags & FLAG_EXTD) != 0, bytes(data[:dlc])

    def _audit(self, can_id, data):
        if self._log is None:
            return
        pci = data[0] >> 4 if data else -1
        rec = {"id": "0x%08X" % can_id,
               "pci": {0: "SF", 1: "FF", 2: "CF", 3: "FC"}.get(pci, "?")}
        if pci == 0 and len(data) >= 2:
            rec["service"] = "0x%02X" % data[1]
        with self._lock:
            self._log.write(json.dumps(rec) + "\n")
            self._log.flush()

    # -- ISO-TP reassembly (requests are single-frame in practice) ----
    def _read_request(self, conn):
        """Returns (payload_bytes, client_can_id) or (None, None)."""
        first = self._recv_frame(conn)
        if first is None:
            return None, None
        can_id, extd, data = first
        if extd != self._extd:
            return None, None
        if can_id not in (self.func_id, self.phys_id) or not data:
            return None, None
        self._audit(can_id, data)
        ptype = data[0] >> 4
        if ptype == 0:  # single frame
            return bytes(data[1:1 + (data[0] & 0x0F)]), can_id
        if ptype == 1:  # first frame: answer with CTS then read CFs
            total = ((data[0] & 0x0F) << 8) | data[1]
            payload = bytes(data[2:8])
            # flow control back to the physical request id
            self._send(conn, self.phys_id, [0x30, 0x00, 0x00, 0, 0, 0, 0, 0])
            seq = 1
            while len(payload) < total:
                fr = self._recv_frame(conn)
                if fr is None:
                    return None, None
                _, d = fr
                if (d[0] >> 4) != 2 or (d[0] & 0x0F) != (seq & 0x0F):
                    return None, None
                payload += bytes(d[1:8])
                seq += 1
            return payload[:total], can_id
        return None, None

    def _send_response(self, conn, payload):
        """ISO-TP encode; waits for the client's flow control on multi-frame."""
        payload = bytes(payload)
        if len(payload) <= 7:
            self._send(conn, self.resp_id,
                       [len(payload)] + list(payload))
            return
        total = len(payload)
        self._send(conn, self.resp_id,
                   [0x10 | ((total >> 8) & 0x0F), total & 0xFF] +
                   list(payload[:6]))
        # wait for flow control (client -> functional or physical id)
        conn.settimeout(2.0)
        try:
            fr = self._recv_frame(conn)
        finally:
            conn.settimeout(None)
        if fr is None:
            return
        can_id, extd, data = fr
        if extd != self._extd:
            return
        if can_id not in (self.func_id, self.phys_id):
            return
        self._audit(can_id, data)
        if not data or (data[0] >> 4) != 3:
            return
        seq, off = 1, 6
        while off < total:
            chunk = payload[off:off + 7]
            self._send(conn, self.resp_id, [0x20 | (seq & 0x0F)] + list(chunk))
            off += len(chunk)
            seq += 1

    # -- service handlers ----------------------------------------------
    def _handle(self, conn, payload):
        if not payload:
            return
        sid = payload[0]
        if sid == 0x09 and len(payload) >= 2 and payload[1] == 0x02:
            vin = [0x49, 0x02, 0x01] + [ord(c) for c in self.vin]
            self._send_response(conn, vin)
        elif sid in (0x03, 0x07, 0x0A):
            out = [sid + 0x40]
            for dtc in self.dtcs[sid]:
                out += encode_dtc(dtc)
            if not self.dtcs[sid]:
                out += [0x00]
            self._send_response(conn, out)
        elif sid == 0x01 and len(payload) >= 2:
            pid = payload[1]
            if pid == 0x00:
                self._send_response(conn, [0x41, 0x00] + support_bitmap(self.pids))
            elif pid in self.pids:
                self._send_response(conn, [0x41, pid] + list(self.pids[pid]))
            # else: stay silent, like a real ECU (client must time out)
        elif sid == 0x02 and len(payload) >= 3 and payload[2] == 0x00:
            # Freeze frame (frame 0): the snapshot stored when the DTC set.
            pid = payload[1]
            if pid == 0x02:
                # PID 02 inside mode 02: the DTC that triggered the frame.
                dtcs = self.dtcs[0x03]
                data = encode_dtc(dtcs[0]) if dtcs else [0x00, 0x00]
                self._send_response(conn, [0x42, pid, 0x00] + data)
            elif pid in self.pids:
                # The sim has no time machine: frozen values equal the
                # current ones. Documented simulator limitation.
                self._send_response(conn, [0x42, pid, 0x00] +
                                    list(self.pids[pid]))
            # else: stay silent, like a real ECU

    def serve(self, conn):
        try:
            while True:
                payload, _ = self._read_request(conn)
                if payload is None:
                    return
                self._handle(conn, payload)
        except (ConnectionResetError, BrokenPipeError, socket.timeout):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass


def run_server(vehicle_path, port, frame_log_path=None, ready=None,
               max_conn=0, variant=11):
    vehicle = json.load(open(vehicle_path))
    flog = open(frame_log_path, "w") if frame_log_path else None
    ecu = Ecu(vehicle, flog, variant=variant)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(4)
    if ready is not None:
        ready["port"] = srv.getsockname()[1]
        ready["event"].set()
    n = 0
    try:
        while max_conn == 0 or n < max_conn:
            conn, _ = srv.accept()
            n += 1
            th = threading.Thread(target=ecu.serve, args=(conn,), daemon=True)
            th.start()
    finally:
        srv.close()
        if flog:
            flog.close()


# ---------------------------------------------------------------- self-test --

def _raw_transact(port, req_payload, expect_frames=1, variant=11):
    """Minimal ISO-TP client used only by --self-test."""
    if variant == 29:
        func_id, resp_id, phys_id = FUNC_ID_29B, RESP_ID_29B, PHYS_ID_29B
        flags = FLAG_EXTD
    else:
        func_id, resp_id, phys_id = FUNC_ID_11B, RESP_ID_11B, PHYS_ID_11B
        flags = 0
    s = socket.create_connection(("127.0.0.1", port), timeout=3)
    s.sendall(FRAME.pack(func_id, len(req_payload) + 1, flags,
                         bytes([len(req_payload)] + list(req_payload)).ljust(8, b"\x00")))

    def recv():
        buf = b""
        while len(buf) < FRAME.size:
            c = s.recv(FRAME.size - len(buf))
            if not c:
                raise AssertionError("connection closed")
            buf += c
        can_id, dlc, rflags, data = FRAME.unpack(buf)
        assert can_id == resp_id, hex(can_id)
        assert (rflags & FLAG_EXTD) == flags, "extd flag mismatch"
        return bytes(data[:dlc])

    data = recv()
    ptype = data[0] >> 4
    if ptype == 0:
        s.close()
        return bytes(data[1:1 + (data[0] & 0x0F)])
    assert ptype == 1, "expected first frame"
    total = ((data[0] & 0x0F) << 8) | data[1]
    payload = bytes(data[2:])
    # flow control to the physical id, like the firmware does
    s.sendall(FRAME.pack(phys_id, 8, flags, bytes([0x30, 0, 0, 0, 0, 0, 0, 0])))
    seq = 1
    while len(payload) < total:
        d = recv()
        assert (d[0] >> 4) == 2 and (d[0] & 0x0F) == (seq & 0x0F)
        payload += bytes(d[1:])
        seq += 1
    s.close()
    return payload[:total]


def self_test():
    ready = {"event": threading.Event()}
    th = threading.Thread(target=run_server,
                          kwargs={"vehicle_path": DEFAULT_VEHICLE, "port": 0,
                                  "ready": ready, "max_conn": 0},
                          daemon=True)
    th.start()
    ready["event"].wait(5)
    port = ready["port"]
    vehicle = json.load(open(DEFAULT_VEHICLE))
    checks = []

    def check(name, cond):
        checks.append((name, bool(cond)))

    # VIN: multi-frame with flow control
    vin = _raw_transact(port, [0x09, 0x02])
    check("vin sid/pid", vin[:3] == bytes([0x49, 0x02, 0x01]))
    check("vin value", vin[3:].decode() == vehicle["vin"])

    # confirmed DTCs
    dtc = _raw_transact(port, [0x03])
    check("dtc sid", dtc[0] == 0x43)
    check("dtc P0171", dtc[1:3] == bytes([0x01, 0x71]))

    # pending: empty -> single 0x00 byte
    pend = _raw_transact(port, [0x07])
    check("pending empty", pend == bytes([0x47, 0x00]))

    # permanent: empty
    perm = _raw_transact(port, [0x0A])
    check("permanent empty", perm == bytes([0x4A, 0x00]))

    # PID
    rpm = _raw_transact(port, [0x01, 0x0C])
    check("rpm", rpm[:2] == bytes([0x41, 0x0C]) and
          (rpm[2] * 256 + rpm[3]) / 4.0 == 1726.0)

    # unsupported PID: ECU stays silent -> client-side timeout
    s = socket.create_connection(("127.0.0.1", port), timeout=3)
    s.sendall(FRAME.pack(FUNC_ID_11B, 3, 0, bytes([0x02, 0x01, 0xFF] + [0] * 5)))
    s.settimeout(0.4)
    try:
        s.recv(FRAME.size)
        check("unsupported pid silent", False)
    except socket.timeout:
        check("unsupported pid silent", True)
    s.close()

    # 0100 bitmap covers 0x0C
    bm = _raw_transact(port, [0x01, 0x00])
    check("0100 bitmap", bm[0] == 0x41 and bm[1] == 0x00 and (bm[2] & (1 << 4)))

    # freeze frame (mode 02): RPM snapshot + triggering DTC
    ff = _raw_transact(port, [0x02, 0x0C, 0x00])
    check("freeze rpm", ff[:3] == bytes([0x42, 0x0C, 0x00]) and
          (ff[3] * 256 + ff[4]) / 4.0 == 1726.0)
    ffd = _raw_transact(port, [0x02, 0x02, 0x00])
    check("freeze dtc", ffd[:3] == bytes([0x42, 0x02, 0x00]) and
          ffd[3:5] == bytes([0x01, 0x71]))

    # 29-bit variant: VIN + DTC over extended ids
    ready29 = {"event": threading.Event()}
    th29 = threading.Thread(target=run_server,
                            kwargs={"vehicle_path": DEFAULT_VEHICLE, "port": 0,
                                    "ready": ready29, "max_conn": 0,
                                    "variant": 29},
                            daemon=True)
    th29.start()
    ready29["event"].wait(5)
    port29 = ready29["port"]
    vin29 = _raw_transact(port29, [0x09, 0x02], variant=29)
    check("29-bit vin", vin29[:3] == bytes([0x49, 0x02, 0x01]) and
          vin29[3:].decode() == vehicle["vin"])
    dtc29 = _raw_transact(port29, [0x03], variant=29)
    check("29-bit dtc", dtc29[0] == 0x43 and dtc29[1:3] == bytes([0x01, 0x71]))

    failed = [n for n, ok in checks if not ok]
    print("can_ecu_sim self-test: %d/%d passed" % (len(checks) - len(failed), len(checks)))
    for n in failed:
        print("  FAIL:", n)
    return 1 if failed else 0


# --------------------------------------------------------------------- main --

def main(argv):
    if "--self-test" in argv:
        return self_test()
    vehicle, port, frame_log, variant = DEFAULT_VEHICLE, 35001, None, 11
    i = 0
    while i < len(argv):
        if argv[i] == "--vehicle":
            vehicle = argv[i + 1]; i += 2
        elif argv[i] == "--port":
            port = int(argv[i + 1]); i += 2
        elif argv[i] == "--frame-log":
            frame_log = argv[i + 1]; i += 2
        elif argv[i] == "--variant":
            variant = int(argv[i + 1]); i += 2
        else:
            i += 1
    print("can_ecu_sim: vehicle=%s port=%d variant=%d" %
          (os.path.basename(vehicle), port, variant), flush=True)
    run_server(vehicle, port, frame_log, variant=variant)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
