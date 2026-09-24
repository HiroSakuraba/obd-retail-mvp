# Web bench simulator

`index.html` — a self-contained virtual car + ELM327 adapter that runs by just
opening the file in a browser. No server, no dependencies, no network.

## What it is

A software car for bench-testing the app with no hardware. Four panels:

1. **Vehicle picker** — 7 vehicles: one healthy 2018 Ford Escape plus one
   faulty vehicle per diagnostic graph (P0171, P0302, P0128, P0420, P0442,
   P0133). Each has a realistic 17-char VIN, stored DTCs, and a physical
   model (base PID values + fault effects).
2. **Live PID dashboard** — RPM, coolant, STFT/LTFT, MAF, MAP, throttle,
   upstream/downstream O₂, ticking 2×/sec with noise and cross-correlations.
   An RPM slider (750–3,000) lets you rev to 2,500 and watch P0171 fuel trims
   fall — the actual diagnostic test. Engine on/off toggle.
3. **ELM327 terminal** — type raw commands, get byte-accurate replies with
   echo and `>` prompts: `ATZ/ATE0/ATE1/ATL0/1/ATS0/1/ATH0/1/ATSP0/6/ATDP/ATHV`,
   `0902` (multi-frame VIN), `03/07/0A` (DTCs per SAE J2012), `0100/0120`
   (PID bitmaps), live PIDs `0104/0105/0106/0107/010B/010C/010D/010E/0110/0111/
   0114/0115/011F/0121/0133` encoded per SAE J1979 from the *current dashboard
   state*, `0202` (frozen snapshot from when the DTC set), `?` for unknowns.
4. **Scripted app scan** — "Run scan like the app" executes the exact command
   sequence the Android app will use (init → VIN → DTCs → required PIDs,
   150 ms apart), then renders a summary card: vehicle, DTCs, key evidence,
   and which `diagnostic-graphs/*.json` each DTC maps to.

## How it relates to `elm327_sim.py`

`elm327_sim.py` is the headless TCP version for automated testing and CI.
This page is the human version: same ELM327 behavior, plus a live vehicle
you can watch and rev. PID encode/decode math lives in one place (`PIDS`)
shared by the dashboard and terminal — the single-source-of-truth rule, so
the terminal can never disagree with the dashboard.

## Verification

The inline script is structured so the core (`respondTo`, PID/DTC codecs,
vehicle physics) is DOM-free and unit-testable under node:
`node --check` clean, 74/74 logic tests pass (DTC vectors P0171→`01 71` etc.,
VIN reassembly, PID round-trips incl. `010C`→`41 0C 1A F8` at 1,726 rpm,
freeze frame, `?`/`NO DATA` paths, fault-physics assertions), plus a DOM-stub
smoke test driving the picker, slider, terminal, and scripted scan end to
end. Works from `file://` — zero external requests.

## Real-adapter BLE scan page

`ble-adapter-scan.html` — the browser equivalent of the ELM327 client for
**real hardware**. Pair a phone or laptop with a real BLE OBD-II adapter and
run the product's read-only scan sequence against a real car.

- **WebBluetooth connect flow** — "Scan for adapter" → device chooser →
  GATT → Nordic UART service/characteristics. The service, TX, and RX UUIDs
  are text inputs defaulting to Nordic UART
  (`6e400001-…-cca9e` / `…0002…` / `…0003…`) because ELM327 BLE clones vary —
  paste your adapter's UUIDs if it uses different ones.
- **Read-only by construction** — there is no free-form command input. Only
  the hardcoded allowlist can ever be written (`ATZ/ATE0/ATL0/ATS0/ATH0/
  ATSP0/ATDP/ATHV/0902/03/07/0100/0120` + live PIDs `0104/0105/0106/0107/
  010B/010C/010D/0110/0111/0114/0115`); anything else throws before it
  reaches the radio. RX notifications are reassembled across MTU chunks
  until the `>` prompt arrives, with a 6 s per-command timeout.
- **Results** — VIN (multi-frame reassembly), protocol, battery voltage,
  confirmed + pending DTCs (SAE J2012, incl. `0x47` SID), live PIDs decoded
  per SAE J1979. "Copy evidence JSON" emits
  `{vin, protocol, dtcs:[{code,status}], pids:{…}, scanned_at}` shaped for
  the backend `/diagnosis` path.

### Browser support

| Browser | Works? |
|---|---|
| Chrome / Edge on Android | Yes |
| Chrome / Edge on macOS / Windows | Yes |
| iOS Safari (any iPhone browser) | **No** — WebBluetooth is unsupported; iPhones need the future native app |

You need a **BLE** adapter (Veepeak BLE / BLE+ class). WiFi-only adapters
cannot talk to a browser page. Pairing happens in-page — do not pair in
system Bluetooth settings first.

### Verification

`node --check` clean, zero external requests. 27/27 tests pass under node
with a mocked `navigator.bluetooth`: full VIN reassembly from frames split
mid-frame across notification chunks, 2-DTC decode, `010C` → 1,726 rpm with
the `>` prompt itself split across chunks, echo stripping, per-command
timeout, chooser-cancel error path, and an assertion that every byte written
during the mock scan was an allowlisted command.
