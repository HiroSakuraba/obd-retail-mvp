# Diagnostic graphs

Each file in this directory is one fault family: a JSON evidence graph the
backend engine (and the Android app's local engine) scores deterministically.
Schema: `schema.json`.

## The pipeline

```
OBD bytes → normalized signals → measurement recipes → evidence
    → diagnosis → repair action → vehicle fitment → retailer SKU
```

The graph owns the middle: **evidence → diagnosis → repair action**. It never
names a SKU, a guide, or a video — those are resolved from the repair action
plus the VIN by the fitment service (`backend/app/fitment.py`).

## Measurement recipes (signals)

A graph may not ask for a "conceptual signal" the scanner cannot acquire.
Every `obd_live` test references one or more **measurement recipes** from the
graph's top-level `signals` array:

```json
{
  "signal": "fuel_trim_idle_vs_2500",
  "service": "01",
  "pids": ["06", "07", "0C"],
  "sample_rate_hz": 1,
  "duration_s": 5,
  "conditions": {"note": "warm idle, then a steady 2500 RPM"},
  "derived": ["trim_idle", "trim_2500"],
  "status": "supported"
}
```

`derived` names the fields `when`-rules may read. `status` is `supported`
(the transport can acquire it today — verified by the capability checker
against `backend/app/capabilities.py`, which mirrors the firmware allowlist)
or `planned` (honestly not implemented, e.g. Mode 06). A `replacement` repair
may never be gated on a planned signal alone.

## Scoring model

For every hypothesis `h`:

```
diagnostic_score(h) ∝ prior(h) × Π evidence_multiplier(observation_i | h)
```

Deliberately called a **score**, not a posterior: these are uncalibrated
expert weights, not calibrated probabilities. Two honesty mechanisms keep
them from inflating:

- **evidence_group**: rules that read the *same measurement* share a group.
  Per hypothesis, only the strongest `|log multiplier|` in the group applies;
  the rest are reported as `deduped`. One O₂ trace is one experiment, even if
  two rules read it.
- **code_context**: companion DTCs reshape hypotheses before normalization
  (`boost_if_present` / `reduce_if_present` with a `why`). The engine scores
  *every* graph matching any present code and returns them ranked — P0171+P0174
  is different evidence from P0171 alone.

The outcome dataset ("did this repair fix it?") is what will eventually
calibrate these scores into genuine probabilities.

UI confidence language is deliberately coarse:

- **> 0.75 — strong evidence.** A repair candidate for the top hypothesis may be
  offered (if its `min_confidence` allows it *and* its `requires_any` evidence
  exists).
- **0.45–0.75 — moderate evidence.** Repair candidates with
  `min_confidence: "moderate"` may be offered, flagged as provisional — but a
  `replacement` repair still needs its confirming evidence first.
- **< 0.45 — not enough evidence.** The app must say so and ask for the next
  test. "No recommendation yet" beats a confident wrong part.

## Categorical test outcomes

Guided tests declare `outcomes`, not just `updates_if_yes`. A "no" answer is
evidence too: a coil swap that does *not* move the misfire strongly
down-weights the coil hypothesis. (`updates_if_yes` still works as the legacy
boolean path.)

## Repairs: actions, classes, gating

A repair is `{repair_id, hypothesis, label, action, component,
recommendation_class, requires_any, min_confidence}`.

- **action** (e.g. `replace_intake_pcv_hose`) is machine-readable and
  VIN-agnostic. The fitment service turns it into a SKU.
- **recommendation_class** is `diagnostic` (a next step: "inspect the intake
  system" — guidance, never a part sale) or `replacement` (a part/service).
- **requires_any** lists evidence that must exist before a `replacement`
  repair is offered: test ids (`warmup_curve`) or test outcomes
  (`inspect_intake:leak_found`). A score alone never sells a part. Repairs
  whose evidence is missing appear in the basket's `gated` list so the UI can
  explain why instead of silently omitting them.

## The next-test rule

Tests are ordered cheapest-first (OBD read before a physical check before a
part swap). The next test is the first untested one — highest expected
discrimination at lowest cost. Future versions may weight by expected
information gain; the order in the file is the MVP policy.

## Safety

`safety.stop_if` lists observation flags (e.g. `flashing_mil`, `overheat`) that
end diagnosis immediately. The engine checks every observation for these flags
before scoring and returns a stop message instead of hypotheses. Never put a
safety condition behind a confidence threshold.

## Authoring guide

1. **Family = one DTC set.** List every DTC that should route to the graph in
   `dtcs`. Start with families where standard OBD data can actually discriminate
   causes (see the roadmap in the product doc).
2. **Explanation describes the observation, not a part.** "The ECU is adding
   more fuel than expected" — never "your MAF sensor failed."
3. **Priors sum to 1** and always include an `other` hypothesis (0.10–0.20) so
   the model can admit it hasn't isolated the cause. Priors must leave the
   top hypothesis **below 0.45** — no graph may offer a repair basket before
   the first test is completed.
4. **Every test needs a measurement recipe.** An `obd_live` test without a
   `signals` entry fails the capability check. If the transport cannot acquire
   the data yet, mark the signal `planned` — and make sure no `replacement`
   repair depends on it alone.
5. **Every test needs a discriminator.** A test is only worth adding if at
   least one outcome moves at least one hypothesis by ≥1.5× or ≤0.7×.
   Prefer `obd_live` tests (free, objective) over guided ones.
6. **`when` expressions** use observation field names and support
   `+ - * /`, comparisons (`> >= < <= == !=`), and `and`/`or`/`not`.
   Keep them to one line; the engine evaluates them with a tiny parser, not
   `eval`. Every field a rule reads must be in its signals' `derived` lists.
7. **One measurement, one experiment.** If two rules read the same trace,
   tag them with the same `evidence_group` so they cannot multiply
   confidence independently.
8. **Guided tests get categorical outcomes.** Always author the "no" /
   "inconclusive" branches — negative evidence is what separates a
   diagnostic tool from a parts cannon.
9. **Repairs reference hypotheses, not codes** — and actions, not SKUs.
   Each repair names the hypothesis it fixes, declares `action`/`component`,
   a `recommendation_class`, and the `requires_any` evidence that unlocks a
   `replacement` sale. Vehicle-specific part data (SKU, price, guide, video,
   emissions notes) belongs in the fitment service, never here.
10. **Companion codes get code_context.** If P0171+P0174 means something
    different from P0171 alone, say so with a boost/reduce entry and a `why`.
11. **Validate** new graphs with scripted sessions (`backend/tests/test_api.py`
    style): submit the canonical observations and assert the expected
    hypothesis comes out on top — and assert the part you *don't* want sold
    stays gated.
