# Glovebox OBD Retail Diagnostic — MVP

A low-cost, **read-only** OBD-II dongle plus a mobile app that turns a
diagnostic trouble code into a guided repair: what the car detected, what is
most likely wrong, what to test next, exact-fit parts, required tools, DIY
instructions — and an aggregated regional demand signal for a retail partner.

## Product in one paragraph

Plug in the dongle, pair the phone, scan. The app reads the VIN, the confirmed
/ pending / permanent DTCs, and the small set of live PIDs its diagnostic
graphs need. A deterministic evidence graph ranks likely causes
(`diagnostic_score ∝ prior × Π evidence multipliers` — uncalibrated expert
weights, not true probabilities) and picks the cheapest
discriminating test next. When the evidence is strong enough, the app builds a
repair basket: exact-fit parts, tools, consumables, instructions, local stock.
The system records whether the repair worked. **A DTC is evidence, not a parts
recommendation** — "no recommendation yet" always beats a confident wrong part.

## Safety stance

- The dongle firmware is a **strict read-only allowlist**: six OBD services
  (01/02/03/07/09/0A), a fixed PID list, everything else refused. The device
  may transmit only allowlisted diagnostic requests plus the transport-layer
  frames strictly necessary to receive their responses (e.g. ISO-TP flow
  control) — there is no raw CAN write path, deliberately, not by omission.
- Two paths, two guarantees: the commodity ELM327 route is
  **application-enforced** read-only; the production custom dongle is
  **device-enforced** via the firmware allowlist.
- Safety flags (`flashing_mil`, `severe_misfire`, `fuel_leak`, `overheat`)
  end diagnosis immediately with a stop-driving message; they are never gated
  behind a confidence threshold.
- Firmware is signed; the app verifies firmware version and capability flags
  at connect time.

## Repo map

```
obd-retail-mvp/
├── diagnostic-graphs/   Evidence graphs (JSON) + schema + authoring guide
│   ├── p0171.json       Lean bank 1 (from the product doc)
│   ├── p030x.json       Misfire family
│   ├── p0128.json       Thermostat rationality
│   ├── p0420.json       Catalyst efficiency (cat repair needs strong evidence)
│   ├── p0442.json       EVAP small leak (drive-cycle verification notes)
│   └── p0133.json       O2 sensor slow response
├── firmware/            C99 dongle firmware: BLE protocol framing, OBD
│                        allowlist state machine, ISO-TP helpers, power mgmt
├── android/             Gradle project; :core is a pure-Kotlin JVM module
│                        (scan session, BLE client contract, OBD repository,
│                        local diagnostic engine, ELM327 client) with 37 unit
│                        tests running in CI — no Android SDK needed for :core
│   └── elm/            ELM327 adapter path: AT protocol parsers
│                       (Elm327Protocol.kt), BLE session (Elm327BleTransport.kt),
│                       GATT wiring notes — talks to real cars via a
│                       commodity BLE OBD adapter
├── backend/             FastAPI reference implementation + pytest suite
│   ├── app/fitment.py   Mock fitment resolver: (VIN, repair action) → SKU,
│   │                    price, stock, tools, guides (diagnosis never emits SKUs)
│   └── required-signals endpoint: GET /v1/sessions/{id}/required-signals
├── simulator/           stdlib-only vehicle simulators:
│   ├── obd_sim.py       JSON BLE protocol over stdio + canned vehicles
│   ├── elm327_sim.py    ELM327 AT protocol over TCP (bench-tests the elm/ client)
│   └── web/             Virtual car in the browser: live PIDs, ELM327
│                        terminal, scripted app scan (open index.html)
├── .github/workflows/   CI: :core Kotlin tests + pytest + firmware tests
├── docs/                threat model, privacy model, hardware BOM,
│                        M1 bench test plan
└── demo/                Standalone interactive browser demo (open index.html)
```

## Quickstart

**Run the demo** — open `demo/index.html` in any browser. No server, no build.

**Run the backend:**

```bash
python3 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt
.venv/bin/python -m pytest backend/tests/ tests/ -q   # 74 passed

**Run the Kotlin tests:**

```bash
cd android && ./gradlew :core:test   # 37 unit tests (needs network for deps on first run)
```
.venv/bin/python -m uvicorn app.main:app --app-dir backend  # :8000
```

**Run the firmware tests:**

```bash
cd firmware/tests && make test   # 25 checks, 0 failures
```

**Run the vehicle simulator:**

```bash
python3 simulator/obd_sim.py --self-test   # 10 scripted checks passed
printf '%s\n' '{"v":1,"id":"a","op":"read_dtcs_confirmed"}' \
  | python3 simulator/obd_sim.py simulator/canned-vehicles/escape-2018-p0171.json
```

**Run the ELM327 bench simulator** (no hardware needed — exercises the real-car path):

```bash
python3 simulator/elm327_sim.py --self-test   # 44 scripted checks passed
.venv/bin/python -m pytest tests/ -q          # parser + signal-capability vectors
```

**Bench-test the adapter path** — see `docs/bench-test-plan.md` (M1 stages A–C).

## Status

M0 (browser prototype) scaffolding, plus the M1 real-car path: the `elm/`
Kotlin client speaks the ELM327 AT protocol to commodity BLE OBD adapters
(Veepeak-class), with a TCP simulator and parser vectors. A formal
**measurement-recipe layer** sits between the graphs and the wire: graphs
declare the signals they need (service, PID, sample rate, duration, derived
values), and a capability checker fails the build if a graph asks for data no
transport can produce. Scores are `diagnostic_score` (uncalibrated expert
weights, honestly named); correlated evidence from one measurement can't
double-count; repairs are gated by evidence, not just score; graphs output
repair actions and a separate fitment resolver maps (VIN, action) → SKU.
The Android BLE GATT implementation, retailer catalog adapters, and
production hardware remain future milestones.
See `docs/` for the threat model, privacy model, and BOM with certification
caveats.
