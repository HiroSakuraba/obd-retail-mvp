# Privacy model — Glovebox OBD retail diagnostic

## Principle

Collect the minimum data needed to diagnose the car and improve the graphs.
Everything else — especially anything that tracks *where the person goes* —
is out of scope by design, not by policy promise.

## What is stored

| Data | Granularity | Why |
|---|---|---|
| VIN (or fitment identifiers derived from it) | Full VIN at scan time; stored as vehicle configuration (year/make/model/engine/trim) | Exact-fit parts require exact fitment |
| DTCs (confirmed/pending/permanent) + freeze frame | Per session | The diagnostic evidence itself |
| Live PID snapshots needed by the active graph | Per test, per session | Evidence for the likelihood updates |
| Test answers and repair outcome | Per session | Graph improvement, wrong-part-rate metric |
| Coarse region | Metro-level bucket (e.g. `metro_042`), never lat/lon | Retail demand aggregation |

## What is never stored

- Routes, continuous GPS, trip history, speed history.
- Driver behavior profiles or scores.
- Anything from outside the diagnostic flow (contacts, other apps).

The dongle has no GPS receiver and the app requests no location permission
beyond what the OS requires for BLE scanning; region buckets are derived
from coarse, user-supplied or store-context data, never from tracks.

## Analytics: aggregate before exposing

- The retailer-facing demand signal (`GET /v1/analytics/demand`) only
  returns repair families with **≥ 5 sessions** in the window
  (`minimum_privacy_count_met`). Smaller counts are suppressed, never
  rounded-and-shown.
- k ≥ 5 suppression is necessary but not sufficient for a commercial
  regional product: repeated queries and differencing across regions /
  time windows can reveal small-cell information. The roadmap is
  defense-in-depth — minimum cohorts *plus* fixed time buckets (no
  arbitrary windowing), query restrictions (rate limits, no free-form
  slicing), retailer access control on which cuts each partner may see,
  and statistical noise on small counts — rather than exposing raw
  counts to arbitrary queries.
- No per-vehicle or per-user rows are ever exposed to retail partners —
  only counts, baselines, and ratios per region bucket.
- Outcome feedback (did the repair work) is aggregated the same way before
  it can influence graph priors.

**Current endpoint semantics (honest):** today's `/v1/analytics/demand`
counts diagnostic-family sessions over all in-memory sessions — there is
no 7-day filtering despite the `sessions_7d` field name, and no region,
timestamp, fitment, SKU, confidence, outcome, conversion, baseline, or
forecast fields. Treat it as `diagnostic_family_counts`; the full demand
model above is roadmap, not current behavior.

## Retention and control

- Raw sessions: retained only as long as needed for outcome verification
  (target: 90 days), then reduced to aggregates.
- Users can delete their sessions; deletion propagates to aggregates on the
  next aggregation run. Design note: once raw sessions are folded into
  aggregates, "deletion propagates" needs a contribution ledger (per-user
  contribution records that can be subtracted) or a full aggregate
  recomputation path — otherwise the promise is not implementable. Pick
  one before retailer-scale deployment.
- Diagnostic graphs are versioned; a graph update never needs historical
  raw sessions, only the aggregated outcome counts.

## The one-line version

We know what the car reported, what was probably wrong, and whether the fix
worked — in a metro-sized bucket. We do not know where the car has been.
