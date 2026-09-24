# M1 bench test plan — ELM327 BLE adapter path

Goal of M1 (from the product doc): reliable VIN/DTC/PID reads on an OBD
simulator and several vehicles, using a commodity BLE OBD adapter instead of
custom dongle hardware. This plan validates the `android/.../elm/` client
(`Elm327Protocol` + `Elm327Session`) against that criterion.

## Stage A — no hardware: TCP simulator

Run the ELM327 simulator and exercise the full session against it:

```bash
python3 simulator/elm327_sim.py --self-test   # 44 scripted checks
```

What it covers: AT init sequence, echo on/off, headers on/off, VIN in both
PCI-frame and numbered-line styles, confirmed/pending/permanent DTCs, all 15
supported PID decodes, `0100` bitmap, `NO DATA` for unsupported PIDs, freeze
frame `0202`, unknown-AT `?` handling.

**Pass criteria (Stage A):**
- `elm327_sim.py --self-test` passes (44/44).
- `pytest tests/test_elm327_parsers.py` passes (28/28): VIN reassembly,
  DTC decode, and every PID formula verified against fixed vectors.
- The Kotlin in `android/.../elm/` hand-tracks the Python mirror: same
  init sequence, same marker search (`49 02` / `43` / `41 XX`), same
  formulas, same confidence behavior on `NO DATA` (skip, don't fail).

## Stage B — adapter on the bench

Hardware: one commodity BLE OBD adapter (Veepeak BLE+, Carista, or VGate
iCar-class) plus either a bench OBD-II simulator (e.g. an ECU simulator that
answers 0902/03/01 on CAN) or a real parked vehicle with ignition on
(engine off is fine for VIN/DTC reads; PID reads need ignition on).

Procedure:
1. Pair the adapter in the Android BLE scanner; confirm the Nordic UART
   service (`6E400001-…`) is present. If the adapter exposes custom UUIDs,
   record them — the app must fall back to service enumeration (see
   `android/elm/README.md`).
2. Run `Elm327Session.initialize()`; assert every init command gets `OK`.
3. `readVin()` → 17 chars, passes the ISO 3779 check, matches the door-jamb VIN.
4. `readDtcs()` / pending / permanent → matches a reference scan tool
   (phone app or handheld scanner) on the same vehicle, including the
   **empty case** (`43 00` → `[]`) on a vehicle with no stored codes.
5. `readPids([0x05, 0x0C, 0x0D, 0x10, ...])` → values sane (coolant within
   ambient..105 °C, RPM 0 at rest, speed 0 at rest, MAF ≥ 0).
6. Pull the adapter mid-session → `ElmTimeoutException`, then
   `initialize()` again reconnects cleanly (reconnect handling).
7. Request an unsupported PID (e.g. `0xFF`) → skipped in the result map,
   session continues (NO DATA handling).
8. Negative check: capture the BLE traffic (nRF Connect log or HCI snoop)
   and confirm **no AT command outside the init sequence and the five read
   builders was ever transmitted** — the read-only allowlist holds on the
   wire, not just in code review.

**Pass criteria (Stage B):** all 8 steps pass on the bench rig.

### Stage B, step 5b — freeze frame (Mode 02) [TODO: Kotlin not yet wired]

The firmware allowlist and the Python/TCP simulator already support Mode 02
(`read_freeze_frame` → service `0x02`, same PID allowlist as Mode 01;
`0202` returns the frozen snapshot). The Android side still needs:

- `Elm327Protocol` / `Elm327BleTransport`: a `readFreezeFrame()` that sends
  `0202` and parses the `42 02 <DTC> <PID><data>…` payload into
  `{dtc: String, pids: Map<String, Double>}` using the existing Mode-01 PID
  decoders (a frozen PID decodes exactly like a live one);
- `VehicleSession`: carry the freeze frame as a first-class object next to
  `dtcs` (do not merge it into live PID values);
- backend already accepts it at `POST /v1/sessions/{id}/freeze-frame` and
  echoes it in `/diagnosis`.

The ELM327 allowed-command set for the commodity path is therefore:
`0902`, `03`, `07`, `0A`, `0202`, and `01xx` for allowlisted PIDs — plus the
AT init sequence. Nothing else may be transmitted.

## Stage C — 5-vehicle checklist (M1 release criterion)

Repeat Stage B steps 2–5 on five vehicles spanning: two makes (e.g. Ford +
Toyota), one pre-2008 J1850 vehicle if available (adapter auto-protocol via
`ATSP0`), one diesel, one hybrid. Record per vehicle: VIN match, DTC match
vs reference tool, PID sanity, any `NO DATA` PIDs.

**M1 is met when:** Stage C passes on all five with zero allowlist
violations in the traffic captures.

## Explicit non-goals

- No write/actuation testing: no service 04 (clear DTCs), no mode 08, no
  custom CAN frames. The adapter is never asked to transmit anything but
  the init/read set.
- No permanent-code clearing, no readiness-drive cycles, no performance
  benchmarking beyond the 5 s response timeout.
- Bench only: nothing here authorizes road testing while reading codes.
