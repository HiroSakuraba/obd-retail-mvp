"""Deterministic diagnostic engine: graph scoring + next-test + repair basket.

Mirrors android/.../diagnosis/DiagnosticEngine.kt so the app and the backend
agree exactly. Scoring: posterior(h) ∝ prior(h) × Π likelihood multipliers.
"""
from __future__ import annotations

import json
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
            v = 1.0 if (v != 0.0 or self._and() != 0.0) else 0.0
        return v

    def _and(self):
        v = self._cmp()
        while self.peek() == "and":
            self.next()
            v = 1.0 if (v != 0.0 and self._cmp() != 0.0) else 0.0
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


# ------------------------------------------------------------------ engine ---
def load_graphs() -> dict:
    graphs = {}
    for p in sorted(GRAPH_DIR.glob("*.json")):
        if p.name == "schema.json":
            continue
        g = json.loads(p.read_text())
        graphs[g["family"]] = g
    return graphs


GRAPHS = load_graphs()


def graph_for_dtcs(dtcs: list) -> dict | None:
    for g in GRAPHS.values():
        if any(d in g.get("dtcs", []) for d in dtcs):
            return g
    return None


def confidence_band(p: float) -> str:
    if p > 0.75:
        return "strong"
    if p >= 0.45:
        return "moderate"
    return "insufficient"


def score(graph: dict, observations: dict, flags: set) -> dict:
    """observations: test_id -> {field: number}; guided tests use answer 1/0."""
    safety = graph.get("safety", {})
    for f in flags:
        if f in safety.get("stop_if", []):
            return {"stopped": True,
                    "stop_message": safety.get("stop_message", "Stop driving."),
                    "hypotheses": []}
    weights = {h["id"]: float(h["prior"]) for h in graph["initial_hypotheses"]}
    labels = {h["id"]: h.get("label", h["id"]) for h in graph["initial_hypotheses"]}
    for t in graph.get("tests", []):
        obs = observations.get(t["id"])
        if obs is None:
            continue
        if t["kind"] == "obd_live":
            for rule in t.get("result_rules", []):
                try:
                    matched = eval_rule(rule["when"], obs)
                except (ValueError, ZeroDivisionError):
                    matched = False
                if matched:
                    for hid, m in rule["updates"].items():
                        weights[hid] = weights.get(hid, 0.0) * float(m)
        elif t["kind"] in ("guided_visual", "guided_manual"):
            if float(obs.get("answer", 0)) == 1.0:
                for hid, m in t.get("updates_if_yes", {}).items():
                    weights[hid] = weights.get(hid, 0.0) * float(m)
    total = sum(weights.values()) or 1.0
    ranked = sorted(
        ({"id": hid, "label": labels.get(hid, hid), "posterior": w / total}
         for hid, w in weights.items()),
        key=lambda h: -h["posterior"],
    )
    top = ranked[0] if ranked else None
    return {
        "stopped": False,
        "hypotheses": ranked,
        "top": top,
        "confidence": confidence_band(top["posterior"]) if top else "insufficient",
    }


def next_test(graph: dict, observations: dict) -> dict | None:
    for t in graph.get("tests", []):
        if t["id"] not in observations:
            return t
    return None


def repair_basket(graph: dict, scored: dict, observations: dict) -> dict:
    """Basket for the top hypothesis if its confidence band allows it.

    Never offers parts before at least one test has been completed: priors
    alone are never enough evidence for a parts recommendation.
    """
    if (scored.get("stopped") or not scored.get("top")
            or not observations):
        return {"confidence": "insufficient", "items": [],
                "message": "Not enough evidence yet — run the next test."}
    top, band = scored["top"], scored["confidence"]
    order = {"strong": 2, "moderate": 1, "insufficient": 0}
    items = [r for r in graph.get("repairs", [])
             if r["hypothesis"] == top["id"]
             and order[band] >= order[r["min_confidence"]]]
    return {
        "confidence": band,
        "top_hypothesis": top,
        "items": items,
        "message": ("Evidence supports this repair."
                    if items else
                    "Not enough evidence yet — run the next test."),
    }
