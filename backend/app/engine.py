"""Deterministic diagnostic engine: graph scoring + next-test + repair basket.

The Android DiagnosticEngine.kt is required to agree with this engine, and
that parity is enforced by shared golden test vectors, not by prose:
backend/app/golden_vectors.py runs this engine over the representative
cases and writes tests/golden/diagnostic_vectors.json, which both the
Python suite (backend/tests/test_golden_vectors.py) and the Android Gradle
tests assert against. If engine semantics change intentionally, regenerate
the vectors and update both sides together.

Scoring: diagnostic_score(h) ∝ prior(h) × Π evidence multipliers from
completed tests. These scores are UNCALIBRATED expert weights, not calibrated
probabilities: correlated rules from a single measurement are deduplicated
(evidence_group — only the strongest multiplier per hypothesis counts), and
the outcome dataset ("did this repair fix it?") is what will calibrate them
later.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

GRAPH_DIR = Path(__file__).resolve().parent.parent.parent / "diagnostic-graphs"


# ------------------------------------------------- tiny "when" evaluator ---
_TOKEN = re.compile(r"\s*(>=|<=|==|!=|[><+\-*/()]|[A-Za-z_][A-Za-z0-9_]*|\d+(?:\.\d+)?)")


class _Parser:
    def __init__(self, expr: str, fields: dict):
        self.toks = [t for t in _TOKEN.findall(expr) if t.strip()]
        self.i = 0
        self.fields = fields

    def peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else None

    def next(self):
        t = self.peek()
        if t is None:
            raise ValueError("truncated rule")
        self.i += 1
        return t

    def parse(self) -> bool:
        v = self._or()
        if self.i != len(self.toks):
            raise ValueError("trailing tokens in rule")
        return v != 0.0

    def _or(self):
        v = self._and()
        while self.peek() == "or":
            self.next()
            # Parse the right side before combining: Python's `or` would
            # skip it when the left side is truthy, leaving its tokens
            # unconsumed ("trailing tokens in rule" regression).
            rhs = self._and()
            v = 1.0 if (v != 0.0 or rhs != 0.0) else 0.0
        return v

    def _and(self):
        v = self._cmp()
        while self.peek() == "and":
            self.next()
            rhs = self._cmp()
            v = 1.0 if (v != 0.0 and rhs != 0.0) else 0.0
        return v

    def _cmp(self):
        if self.peek() == "not":
            self.next()
            return 1.0 if self._cmp() == 0.0 else 0.0
        l = self._add()
        op = self.peek()
        if op in (">", ">=", "<", "<=", "==", "!="):
            self.next()
            r = self._add()
            ok = {">": l > r, ">=": l >= r, "<": l < r, "<=": l <= r,
                  "==": l == r, "!=": l != r}[op]
            return 1.0 if ok else 0.0
        return l

    def _add(self):
        v = self._mul()
        while self.peek() in ("+", "-"):
            op = self.next()
            r = self._mul()
            v = v + r if op == "+" else v - r
        return v

    def _mul(self):
        v = self._atom()
        while self.peek() in ("*", "/"):
            op = self.next()
            r = self._atom()
            v = v * r if op == "*" else v / r
        return v

    def _atom(self):
        t = self.next()
        if t == "(":
            v = self._or()
            if self.next() != ")":
                raise ValueError("missing )")
            return v
        if t == "-":
            return -self._atom()
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", t):
            if t not in self.fields:
                raise ValueError(f"unknown field {t!r}")
            return float(self.fields[t])
        return float(t)


def eval_rule(expr: str, fields: dict) -> bool:
    return _Parser(expr, fields).parse()


# ------------------------------------------------------------------ graphs ---
def load_graphs() -> dict:
    graphs = {}
    for p in sorted(GRAPH_DIR.glob("*.json")):
        if p.name == "schema.json":
            continue
        g = json.loads(p.read_text())
        graphs[g["family"]] = g
    return graphs


GRAPHS = load_graphs()


def graphs_for_dtcs(dtcs: list) -> list[dict]:
    """Every graph matching ANY of the codes, best match first.

    Real cars carry code clusters (P0171+P0174, P0302+P0171); selecting one
    graph and discarding the rest throws away diagnostic information. The
    engine scores all matched graphs and returns them ranked."""
    codes = set(dtcs)
    matched = [g for g in GRAPHS.values()
               if codes & set(g.get("dtcs", []))]
    matched.sort(key=lambda g: (-len(codes & set(g.get("dtcs", []))),
                                g["family"]))
    return matched


def graph_for_dtcs(dtcs: list) -> dict | None:
    """Primary graph: the best match. Kept for backward compatibility."""
    m = graphs_for_dtcs(dtcs)
    return m[0] if m else None


def confidence_band(p: float) -> str:
    if p > 0.75:
        return "strong"
    if p >= 0.45:
        return "moderate"
    return "insufficient"


def _split_observation(obs: dict) -> tuple[dict, str | None]:
    """Observations are test_id -> {"fields": {...}, "outcome": ...}.

    Accepts the legacy flat shape (test_id -> {field: number}) so old
    sessions and tests keep working."""
    if isinstance(obs, dict) and "fields" in obs and isinstance(obs["fields"], dict):
        return obs["fields"], obs.get("outcome")
    return obs, None


def evidence_satisfied(observations: dict) -> set[str]:
    """Evidence ids usable by requires_any: completed test ids plus
    test_id:outcome for recorded categorical outcomes."""
    sat: set[str] = set()
    for tid, obs in observations.items():
        fields, outcome = _split_observation(obs)
        sat.add(tid)
        if outcome:
            sat.add(f"{tid}:{outcome}")
        elif float(fields.get("answer", 0)) == 1.0:
            sat.add(f"{tid}:yes")
    return sat


def _guided_updates(test: dict, fields: dict, outcome: str | None) -> tuple[dict, str | None]:
    """Resolve a guided test's updates for the recorded outcome.

    Categorical outcomes are preferred: a 'no' answer is evidence too
    (a coil swap that does NOT move the misfire down-weights the coil).
    Falls back to the legacy boolean updates_if_yes path."""
    outcomes = test.get("outcomes", {})
    if outcome and outcome in outcomes:
        return outcomes[outcome].get("updates", {}), outcome
    if outcome in (None, "yes") and float(fields.get("answer", 0)) == 1.0:
        if outcome == "yes" and "yes" in outcomes:
            return outcomes["yes"].get("updates", {}), "yes"
        if test.get("updates_if_yes"):
            return test["updates_if_yes"], "yes"
    return {}, outcome


def score(graph: dict, observations: dict, flags: set,
          dtc_evidence: list | None = None,
          all_dtcs: list | None = None) -> dict:
    """Score one graph.

    observations: test_id -> {"fields": {...}, "outcome": ...} (guided
    tests use a categorical outcome; obd_live tests use fields).
    dtc_evidence: [{code, status}] carried from the scan.
    all_dtcs: every code on the vehicle (for code_context); defaults to
    the codes in dtc_evidence.
    """
    safety = graph.get("safety", {})
    for f in flags:
        if f in safety.get("stop_if", []):
            return {"stopped": True,
                    "stop_message": safety.get("stop_message", "Stop driving."),
                    "hypotheses": [],
                    "top": None,
                    "confidence": "insufficient",
                    "dtc_evidence": dtc_evidence or [],
                    "code_context_applied": [],
                    "evidence_deduped": [],
                    "test_outcomes": {}}
    weights = {h["id"]: float(h["prior"]) for h in graph["initial_hypotheses"]}
    labels = {h["id"]: h.get("label", h["id"]) for h in graph["initial_hypotheses"]}
    test_by_id = {t["id"]: t for t in graph.get("tests", [])}
    deduped: list[dict] = []
    test_outcomes: dict[str, str | None] = {}

    for tid, obs in observations.items():
        t = test_by_id.get(tid)
        if t is None:
            continue
        fields, outcome = _split_observation(obs)
        if t["kind"] == "obd_live":
            # One measurement can match several rules (e.g. several
            # readings of the same O2 trace). Rules sharing an
            # evidence_group are NOT independent experiments: per
            # hypothesis only the strongest |log multiplier| applies.
            # group -> hypothesis -> (multiplier, rule_index)
            groups: dict[str, dict[str, tuple[float, int]]] = {}
            for i, rule in enumerate(t.get("result_rules", [])):
                try:
                    matched = eval_rule(rule["when"], fields)
                except (ValueError, ZeroDivisionError):
                    matched = False
                if not matched:
                    continue
                gkey = rule.get("evidence_group") or f"rule:{i}"
                bucket = groups.setdefault(gkey, {})
                for hid, m in rule["updates"].items():
                    m = float(m)
                    prev = bucket.get(hid)
                    if prev is None or abs(math.log(m)) > abs(math.log(prev[0])):
                        if prev is not None:
                            deduped.append({
                                "test_id": tid, "evidence_group": gkey,
                                "hypothesis": hid,
                                "dropped_multiplier": prev[0],
                                "kept_multiplier": m,
                                "reason": "same measurement as a stronger rule",
                            })
                        bucket[hid] = (m, i)
                    else:
                        deduped.append({
                            "test_id": tid, "evidence_group": gkey,
                            "hypothesis": hid,
                            "dropped_multiplier": m,
                            "kept_multiplier": prev[0],
                            "reason": "same measurement as a stronger rule",
                        })
            for bucket in groups.values():
                for hid, (m, _i) in bucket.items():
                    weights[hid] = weights.get(hid, 0.0) * m
        elif t["kind"] in ("guided_visual", "guided_manual"):
            updates, resolved = _guided_updates(t, fields, outcome)
            test_outcomes[tid] = resolved
            for hid, m in updates.items():
                weights[hid] = weights.get(hid, 0.0) * float(m)

    # Companion codes reshape the hypotheses (P0171+P0174 is different
    # evidence from P0171 alone).
    code_context_applied: list[dict] = []
    codes = set(all_dtcs) if all_dtcs is not None else \
        {e["code"] for e in (dtc_evidence or [])}
    for entry in (graph.get("code_context", {}).get("boost_if_present", [])
                  + graph.get("code_context", {}).get("reduce_if_present", [])):
        if entry["dtc"] in codes and entry["hypothesis"] in weights:
            weights[entry["hypothesis"]] *= float(entry["multiplier"])
            code_context_applied.append(entry)

    total = sum(weights.values()) or 1.0
    ranked = sorted(
        ({"id": hid, "label": labels.get(hid, hid), "diagnostic_score": w / total}
         for hid, w in weights.items()),
        key=lambda h: -h["diagnostic_score"],
    )
    top = ranked[0] if ranked else None
    return {
        "stopped": False,
        "hypotheses": ranked,
        "top": top,
        "confidence": confidence_band(top["diagnostic_score"]) if top else "insufficient",
        "dtc_evidence": dtc_evidence or [],
        "code_context_applied": code_context_applied,
        "evidence_deduped": deduped,
        "test_outcomes": test_outcomes,
    }


def score_all(graphs: list[dict], observations: dict, flags: set,
              dtc_evidence: list | None = None) -> list[dict]:
    """Score every matched graph; best primary diagnosis first."""
    all_dtcs = [e["code"] for e in (dtc_evidence or [])]
    out = []
    for g in graphs:
        s = score(g, observations, flags, dtc_evidence=dtc_evidence,
                  all_dtcs=all_dtcs)
        s["family"] = g["family"]
        s["title"] = g["title"]
        out.append(s)
    out.sort(key=lambda s: (-(s["top"]["diagnostic_score"] if s["top"] else 0.0),
                            s["family"]))
    return out


def next_test(graphs: list[dict], observations: dict) -> dict | None:
    for g in graphs:
        for t in g.get("tests", []):
            if t["id"] not in observations:
                return t
    return None


# ------------------------------------------------------------------ basket ---
def _repair_view(r: dict) -> dict:
    return {
        "repair_id": r["repair_id"],
        "hypothesis": r["hypothesis"],
        "label": r["label"],
        "action": r["action"],
        "component": r["component"],
        "recommendation_class": r["recommendation_class"],
        "min_confidence": r["min_confidence"],
        "verification": r.get("verification"),
        "tools": r.get("tools", []),
        "consumables": r.get("consumables", []),
    }


def repair_basket(graph: dict, scored: dict, observations: dict) -> dict:
    """Basket for the top hypothesis, split by recommendation class.

    diagnostic-class entries are GUIDANCE ("inspect the intake next") —
    never a part sale. replacement-class entries are offered only when the
    score band is reached AND requires_any evidence exists: a score alone
    never sells a part. Repairs whose evidence is missing are reported in
    `gated` so the UI can say why, instead of silently omitting them.
    """
    if (scored.get("stopped") or not scored.get("top")
            or not observations):
        return {"confidence": "insufficient", "items": [], "guidance": [],
                "gated": [],
                "message": "Not enough evidence yet — run the next test."}
    top, band = scored["top"], scored["confidence"]
    order = {"strong": 2, "moderate": 1, "insufficient": 0}
    sat = evidence_satisfied(observations)
    items, guidance, gated = [], [], []
    for r in graph.get("repairs", []):
        if r["hypothesis"] != top["id"]:
            continue
        if order[band] < order[r["min_confidence"]]:
            continue
        if r["recommendation_class"] == "diagnostic":
            guidance.append(_repair_view(r))
            continue
        reqs = r.get("requires_any", [])
        hit = [q for q in reqs if q in sat]
        if hit:
            items.append({**_repair_view(r), "satisfied_evidence": hit})
        else:
            gated.append({"repair_id": r["repair_id"], "label": r["label"],
                          "missing_evidence": reqs,
                          "reason": "score band reached but the confirming "
                                    "evidence has not been recorded yet"})
    if items:
        message = "Evidence supports this repair."
    elif guidance:
        message = ("Next step: " + "; ".join(g["label"] for g in guidance)
                   + ". No part is recommended until the confirming evidence "
                     "is recorded.")
    else:
        message = "Not enough evidence yet — run the next test."
    return {
        "confidence": band,
        "top_hypothesis": top,
        "items": items,
        "guidance": guidance,
        "gated": gated,
        "message": message,
    }


def required_signals(graphs: list[dict], observations: dict) -> list[dict]:
    """Union of measurement recipes across the matched graphs, with
    acquisition state. This is the contract the app calls before scanning:
    it tells the OBD layer exactly what to measure and how."""
    seen: dict[str, dict] = {}
    for g in graphs:
        declared = {s["signal"]: s for s in g.get("signals", [])}
        for t in g.get("tests", []):
            if t["kind"] != "obd_live":
                continue
            for sig_id in t.get("signals", []):
                if sig_id in seen or sig_id not in declared:
                    continue
                recipe = dict(declared[sig_id])
                recipe["test_id"] = t["id"]
                recipe["family"] = g["family"]
                recipe["acquired"] = t["id"] in observations
                seen[sig_id] = recipe
    return list(seen.values())
