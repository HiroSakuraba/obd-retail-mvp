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

## Stage A2 — dongle firmware on the simulator (done)

The custom-dongle firmware path (`firmware/esp32/`) is validated with no
hardware, against the CAN-frame-level ECU simulator instead of the ELM327
text-protocol simulator:

```bash
python3 simulator/can_ecu_sim.py --self-test   # 13/13 (incl. Mode 02 + 29-bit)
make -C firmware/esp32/tests check              # 81/81 (incl. 29-bit phase)
make -C firmware/esp32/tests check-app          # ESP-IDF app stub-compile
```

What it covers: the real `obd_client.c` (the file that ships on the
ESP32) does multi-frame VIN with ISO-TP flow control, confirmed/pending/
permanent DTC decode per SAE J2012 on two canned vehicles (Escape P0171;
Camry P0420 with non-empty pending P0133 + permanent P0420), 15 live PIDs
against exact canned values, Mode 02 freeze frames (snapshot values +
triggering DTC), graceful skip on ECU silence, and a **wire audit**
proving the client transmitted nothing but allowlisted services
(`01/02/03/07/09/0A`) and ISO-TP flow-control frames. A third phase runs
the whole client against a 29-bit simulator, exercising the variant-aware
request/response id mapping (`0x18DB33F1`/`0x18DAF110`). A non-allowlisted
PID is refused with zero frames transmitted. `check-app` compiles the
actual ESP-IDF application sources (`main_esp32.c`, `twai_transport.c`,
`ble_gatt.c`) with `-DESP_PLATFORM` against stub headers — the
compile-coverage the first external review found missing.

**Pass criteria (Stage A2):** all three commands green. (All are: 24 Sept 2026.)

## Stage B2 — dongle firmware on the bench

Hardware: the bench build from `hardware/build-guide.html` (ESP32-C3 +
TCAN transceiver, GPIO4 TX / GPIO5 RX) plus either a bench ECU simulator
that answers 0902/03/07/0A/01/02 on CAN or a real parked vehicle.

Narrow milestone (the first thing that must work end to end): ESP32-C3 →
TCAN → sim CAN ECU → BLE phone → VIN + DTC + RPM, using the exact binary
built in CI. Everything else is secondary until that chain is green.

Procedure:
1. Flash `firmware/esp32` (`pio run -t upload`); confirm `Glovebox-OBD`
   advertises and the Nordic UART service is present.
2. `full_scan` over BLE → VIN matches the door jamb; DTC lists match a
   reference scan tool, including the empty case.
3. Live PIDs sane (coolant within ambient..105 °C, RPM 0 at rest).
   Freeze frame: `read_freeze_frame` for a stored DTC returns the Mode 02
   snapshot with the triggering DTC.
4. Pull the transceiver's CANH/CANL mid-scan → client times out cleanly,
   reconnects on the next request (no wedge).
5. Negative check: capture the CAN traffic and confirm **no frame outside
   functional requests with services 01/02/03/07/09/0A and ISO-TP flow
   control to the physical id was ever transmitted** — the read-only rule
   on the wire, not just in code review. Repeat with a 29-bit ECU if
   available: the firmware must auto-detect the variant (extended ids)
   and still pass steps 2–4.
6. Replies over 180 bytes (e.g. `full_scan`) arrive as chunk envelopes
   (`chunk`/`chunks`/`b64`) and reassemble to the full JSON on the phone.

**Pass criteria (Stage B2):** all 6 steps pass on the bench rig.

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
