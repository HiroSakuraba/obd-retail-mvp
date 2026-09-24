# Glovebox OBD Retail Diagnostic — MVP

A low-cost, **read-only** OBD-II dongle plus a mobile app that turns a
diagnostic trouble code into a guided repair: what the car detected, what is
most likely wrong, what to test next, exact-fit parts, required tools, DIY
instructions — and an aggregated regional demand signal for a retail partner.

## Product in one paragraph

Plug in the dongle, pair the phone, scan. The app reads the VIN, the confirmed
/ pending / permanent DTCs, and the small set of live PIDs its diagnostic
graphs need. A deterministic evidence graph ranks likely causes
(`posterior ∝ prior × Π likelihood multipliers`) and picks the cheapest
discriminating test next. When the evidence is strong enough, the app builds a
repair basket: exact-fit parts, tools, consumables, instructions, local stock.
The system records whether the repair worked. **A DTC is evidence, not a parts
recommendation** — "no recommendation yet" always beats a confident wrong part.

## Safety stance

- The dongle firmware is a **strict read-only allowlist**: five OBD services
  (01/03/07/09/0A), a fixed PID list, everything else refused. There is no raw
  CAN write path — deliberately, not by omission.
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
│   └── p0128.json       Thermostat rationality
├── firmware/            C99 dongle firmware: BLE protocol framing, OBD
│                        allowlist state machine, ISO-TP helpers, power mgmt
├── android/             Kotlin skeletons (pure Kotlin, no Android SDK deps):
│                        scan session, BLE client contract, OBD repository,
│                        local diagnostic engine
├── backend/             FastAPI reference implementation + pytest suite
├── simulator/           stdlib-only vehicle simulator speaking the JSON BLE
│                        protocol over stdio + canned vehicles
├── docs/                threat model, privacy model, hardware BOM
└── demo/                Standalone interactive browser demo (open index.html)
```

## Quickstart

**Run the demo** — open `demo/index.html` in any browser. No server, no build.

**Run the backend:**

```bash
python3 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt
.venv/bin/python -m pytest backend/tests/ -q        # 3 passed
.venv/bin/python -m uvicorn app.main:app --app-dir backend  # :8000
```

**Run the firmware tests:**

```bash
cd firmware/tests && make test   # 21 checks, 0 failures
```

**Run the vehicle simulator:**

```bash
python3 simulator/obd_sim.py --self-test   # 10 scripted checks passed
printf '%s\n' '{"v":1,"id":"a","op":"read_dtcs_confirmed"}' \
  | python3 simulator/obd_sim.py simulator/canned-vehicles/escape-2018-p0171.json
```

## Status

M0 (browser prototype) scaffolding. The diagnostic graphs, scoring engine, BLE
protocol, and allowlist are real and tested; the Android BLE implementation,
retailer catalog adapters, and production hardware are future milestones.
See `docs/` for the threat model, privacy model, and BOM with certification
caveats.
