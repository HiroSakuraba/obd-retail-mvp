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
