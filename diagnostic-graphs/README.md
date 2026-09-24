# Diagnostic graphs

Each file in this directory is one fault family: a JSON evidence graph the
backend engine (and the Android app's local engine) scores deterministically.
Schema: `schema.json`.

## Scoring model

For every hypothesis `h`:

```
posterior(h) ∝ prior(h) × Π likelihood_multiplier(observation_i | h)
```

Each completed test contributes the multipliers from its matching `result_rules`
(`when` expressions evaluated against the submitted observation fields) or from
`updates_if_yes` when the user answers "yes" to a guided test. Posteriors are
renormalized to sum to 1. Hypotheses never named by any update keep multiplier 1.

UI confidence language is deliberately coarse:

- **> 0.75 — strong evidence.** A repair candidate for the top hypothesis may be
  offered (if its `min_confidence` allows it).
- **0.45–0.75 — moderate evidence.** Repair candidates with
  `min_confidence: "moderate"` may be offered, flagged as provisional.
- **< 0.45 — not enough evidence.** The app must say so and ask for the next
  test. "No recommendation yet" beats a confident wrong part.

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
4. **Every test needs a discriminator.** A test is only worth adding if at
   least one outcome moves at least one hypothesis by ≥1.5× or ≤0.7×.
   Prefer `obd_live` tests (free, objective) over guided ones.
5. **`when` expressions** use observation field names and support
   `+ - * /`, comparisons (`> >= < <= == !=`), and `and`/`or`/`not`.
   Keep them to one line; the engine evaluates them with a tiny parser, not
   `eval`.
6. **Repairs reference hypotheses, not codes.** Each repair names the
   hypothesis it fixes, lists exact parts/tools/consumables, and declares
   `min_confidence`. A repair is only offered when the top hypothesis reaches
   that band — otherwise the basket stays empty with a "test more" message.
7. **Validate** new graphs with `backend/tests/test_api.py`-style scripted
   sessions: submit the canonical observations and assert the expected
   hypothesis comes out on top.
