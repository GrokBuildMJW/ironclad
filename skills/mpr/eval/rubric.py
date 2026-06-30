"""MPR eval rubric — dimensions + scoring rules AS DATA (Spec 08 §5.1). Pure, deterministic, unit-tested.

No model logic lives here: the judge (judge.py) supplies per-dimension 0-5 scores; this module aggregates
them (MEDIAN across votes → robust to one outlier judge) and gates against per-dimension floors. The
``cost_latency`` dimension is COMPUTED, not judged (a model cannot measure cost/latency): full marks while
overhead stays within budget, penalised as it exceeds budget, and the penalty is harsher when the
qualitative value-add is low. Stdlib-only (statistics).
"""
from __future__ import annotations

import math
from statistics import median
from typing import Dict, List, Optional

#: §5.1 rubric — weights sum to 1.0, every dimension 0-5.
RUBRIC: Dict[str, dict] = {
    "coverage":           {"weight": 0.30, "scale": "0-5", "desc": "deckt die Referenz-Achsen der Frage ab"},
    "conflict_surfacing": {"weight": 0.20, "scale": "0-5", "desc": "macht divergierende Sichten/Spannungen explizit"},
    "grounding":          {"weight": 0.20, "scale": "0-5", "desc": "Claims belegt (Zitate/Quellen) statt behauptet"},
    "decision_clarity":   {"weight": 0.20, "scale": "0-5", "desc": "klare Matrix + Empfehlung + Rückzug (decision)"},
    "cost_latency":       {"weight": 0.10, "scale": "0-5", "desc": "Mehraufwand vertretbar vs. Mehrwert (Penalty)"},
}
#: the four dimensions a model can judge from the answer text; cost_latency is computed (see below).
JUDGED_DIMS = ["coverage", "conflict_surfacing", "grounding", "decision_clarity"]
_MAX = 5.0


def median_scores(judgements: List[Dict[str, float]]) -> Dict[str, float]:
    """Median per dimension across judge votes (robust to a single outlier). A dim contributes ONLY if a
    strict majority of the votes scored it (quorum = ⌊n/2⌋+1) — a single non-consensus judge must not set
    a dimension's median at full weight. (In the real panel every kept vote scores every JUDGED_DIM, so
    the quorum is always met; this guards a partial/hand-fed input.)"""
    n = len(judgements)
    quorum = (n // 2) + 1 if n else 1
    dims = set()
    for j in judgements:
        dims |= set(j)
    out: Dict[str, float] = {}
    for d in dims:
        vals = [float(j[d]) for j in judgements if j.get(d) is not None]
        if len(vals) >= quorum:
            out[d] = float(median(vals))
    return out


def _overhead(delta: Optional[float], budget: Optional[float]) -> float:
    """One axis' overhead ratio. budget None → axis DISABLED (0); budget <= 0 → ZERO TOLERANCE (any
    positive delta is infinitely over budget); else delta/budget. Distinguishes 'off' from 'no slack'."""
    d = max(0.0, delta or 0.0)
    if budget is None:
        return 0.0
    if budget <= 0:
        return float("inf") if d > 0 else 0.0
    return d / budget


def cost_latency_score(cost_delta_usd: Optional[float], latency_delta_s: Optional[float],
                       value_add: float, *, cost_budget: Optional[float] = 0.5,
                       latency_budget: Optional[float] = 30.0) -> float:
    """0-5: full marks when the A−B overhead is within budget; deducted as overhead exceeds budget, with a
    HARSHER deduction when ``value_add`` (mean of the judged dims, 0-5) is low. Pure, monotone.
    overhead = max(cost_over, latency_over); penalty = overhead·(1 − 0.5·value_factor)·5, clamped to [0,5].
    A budget of 0 means zero tolerance (→ score 0 on any overhead), None disables that axis (see _overhead)."""
    overhead = max(_overhead(cost_delta_usd, cost_budget), _overhead(latency_delta_s, latency_budget))
    value_factor = max(0.0, min(1.0, (value_add or 0.0) / _MAX))
    penalty = overhead * (1.0 - 0.5 * value_factor) * _MAX
    return round(max(0.0, _MAX - penalty), 3)


def weighted_total(scored: Dict[str, float]) -> float:
    """Σ weight·score over the rubric dims present in *scored*, weights RENORMALISED to those present.

    Policy (explicit): the total scores the answer over exactly the dims that were measured. For a
    comparable cross-run total, always pass the full rubric (the four judged dims + a computed
    ``cost_latency``) — omitting a dim renormalises the rest, so a 4-dim total and a 5-dim total are NOT
    on the same scale (by design: more dims measured = a different composite). 'total' itself is filtered
    out (not in RUBRIC) so it never feeds back."""
    num = sum(RUBRIC[d]["weight"] * scored[d] for d in scored if d in RUBRIC)
    den = sum(RUBRIC[d]["weight"] for d in scored if d in RUBRIC)
    return round(num / den, 3) if den else 0.0


def score(judgements: List[Dict[str, float]], *, cost_latency: Optional[float] = None) -> Dict[str, float]:
    """Aggregate judge votes → median per judged dim (+ optional computed ``cost_latency``) + weighted total.

    Reserved (#503 MPR-EVAL-2): the canonical rubric scoring API (``score`` / ``weighted_total``), fully
    unit-tested. The A/B judge gate currently consumes per-dim medians directly rather than the composite
    weighted total, so this composite has no production caller yet — kept as the documented scoring
    contract for the eval harness, not as dead code."""
    scored = median_scores(judgements)
    if cost_latency is not None:
        scored["cost_latency"] = round(float(cost_latency), 3)
    scored["total"] = weighted_total({k: v for k, v in scored.items() if k in RUBRIC})
    return scored


def passes(scored: Dict[str, float], floor: Dict[str, float]) -> bool:
    """True iff every floored dimension meets its floor. FAIL-CLOSED: a missing OR non-finite (NaN/inf)
    scored dim fails (NaN < mn is False, so without the isfinite guard a NaN would silently pass)."""
    for dim, mn in (floor or {}).items():
        v = scored.get(dim)
        if v is None or not math.isfinite(v) or v < mn:
            return False
    return True
