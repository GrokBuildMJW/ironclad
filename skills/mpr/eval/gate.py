"""MPR eval regression-gate (Spec 08 §6) — PURE threshold checks over a judged A/B report + a decline run.

The decline guard (§6.1) consumes the deterministic router decisions; the A/B guards (§6.2) parse a JUDGED
report (harness ``diff_arms`` merged with ``judge_panel`` scores) against ``eval/gate.toml`` thresholds.
Pure + stdlib (tomllib/statistics) → unit-tested with a synthetic report; the LIVE report comes from the
operator A/B + judge run (Merge-Gate §7 stufe 4). Thresholds are tunebar in gate.toml, never in this code.

Judged-report shape (per query_id):
  {"domain": str, "a": {dim: score 0-5}, "b": {dim: score 0-5}, "pairwise": {dim: "a"|"b"},
   "a_cost_usd": float, "b_cost_usd": float}        # 'a' = MPR arm, 'b' = baseline arm
"""
from __future__ import annotations

try:                       # Python 3.11+: stdlib
    import tomllib
except ModuleNotFoundError:  # Python 3.10: the tomli backport (declared dep on <3.11)
    import tomli as tomllib
from pathlib import Path
from statistics import median
from typing import Dict, List, Optional, Tuple

_GATE_TOML = Path(__file__).resolve().parent / "gate.toml"


def load_gate(path: Optional[str] = None) -> dict:
    """Load the tunable thresholds (gate.toml). Stdlib tomllib — no third-party dep."""
    return tomllib.loads((Path(path) if path else _GATE_TOML).read_text(encoding="utf-8"))


def decline_rate(decisions: List[str]) -> float:
    """Fraction of router decisions that are 'decline'. Empty → 0.0 (fail-closed for a §6.1 floor)."""
    return (sum(1 for d in decisions if d == "decline") / len(decisions)) if decisions else 0.0


def _arm_dim_scores(report: Dict[str, dict], arm: str, dim: str) -> List[float]:
    return [q[arm][dim] for q in report.values() if dim in (q.get(arm) or {})]


def pairwise_wins(report: Dict[str, dict]) -> Tuple[int, int]:
    """Raw A/B win COUNTS across ALL judged dims of all queries (ties/omitted dims don't count)."""
    a = sum(1 for q in report.values() for w in (q.get("pairwise") or {}).values() if w == "a")
    b = sum(1 for q in report.values() for w in (q.get("pairwise") or {}).values() if w == "b")
    return a, b


def merge_judged_report(diff: Dict[str, dict], judgements: Dict[str, dict]) -> Dict[str, dict]:
    """Merge harness ``diff_arms`` (per query_id: domain + a/b_cost_usd) with per-query ``judge_panel``
    results ({a, b, pairwise}) into the gate's judged-report shape — making the producer→gate seam CODE,
    not prose. A query missing its judgement → empty a/b/pairwise → the completeness guard in
    check_coverage_ge_baseline fails it (never a silent pass on the judged subset)."""
    out: Dict[str, dict] = {}
    for qid, d in (diff or {}).items():
        j = (judgements or {}).get(qid) or {}
        out[qid] = {"domain": d.get("domain"), "a": j.get("a") or {}, "b": j.get("b") or {},
                    "pairwise": j.get("pairwise") or {},
                    "a_cost_usd": d.get("a_cost_usd"), "b_cost_usd": d.get("b_cost_usd")}
    return out


# ── §6.1 ────────────────────────────────────────────────────────────────────────────────────────
def check_decline_rate(decisions: List[str], gate: dict) -> Tuple[bool, float]:
    rate = decline_rate(decisions)
    return rate >= gate["regression"]["decline_rate_min"], round(rate, 4)


# ── §6.2 ────────────────────────────────────────────────────────────────────────────────────────
def check_coverage_ge_baseline(report: Dict[str, dict], gate: dict) -> Tuple[bool, dict]:
    """Median MPR(A) coverage meets the floor AND does not regress below baseline(B) − epsilon.
    COMPLETENESS (false-pass guard): EVERY query must carry an MPR(A) coverage — a partial report (some
    queries un-judged, or the MPR arm silently stopped covering them) fails-closed, never passes on the
    surviving subset."""
    ab = gate["ab"]
    n = len(report)
    a_vals = _arm_dim_scores(report, "a", "coverage")
    b_vals = _arm_dim_scores(report, "b", "coverage")
    if not report or len(a_vals) < n:
        return False, {"complete": False, "judged": len(a_vals), "queries": n}
    a = float(median(a_vals))
    b = float(median(b_vals)) if b_vals else None
    ok = a >= ab["coverage_floor"] and (b is None or a >= b - ab["coverage_epsilon"])
    return ok, {"complete": True, "a_coverage": a, "b_coverage": b}


def check_cost_within_budget(report: Dict[str, dict], gate: dict) -> Tuple[bool, float]:
    # 0.0 is a legitimate total (the in-engine MVP has no egress cost); the all-failed/un-judged case is
    # caught by the coverage completeness guard, so cost stays a pure runaway ceiling.
    total_a = sum((q.get("a_cost_usd") or 0.0) for q in report.values())
    return total_a <= gate["ab"]["max_cost_usd_per_run"], round(total_a, 6)


def check_pairwise_a_not_worse(report: Dict[str, dict], gate: dict) -> Tuple[bool, dict]:
    a, b = pairwise_wins(report)
    tot = a + b
    if tot == 0:
        return False, {"a_winrate": 0.0, "b_winrate": 0.0, "signal": 0}   # no pairwise signal → fail-closed
    ar, br = a / tot, b / tot
    ok = ar >= br - gate["ab"]["pairwise_epsilon"]
    return ok, {"a_winrate": round(ar, 4), "b_winrate": round(br, 4)}


def gate_report(report: Dict[str, dict], decisions: List[str], gate: dict) -> dict:
    """Run all §6 checks → {'passed': bool, 'checks': {name: {'pass', 'detail'}}}. fail-fast aggregate."""
    checks = {
        "decline_rate": check_decline_rate(decisions, gate),
        "coverage_ge_baseline": check_coverage_ge_baseline(report, gate),
        "cost_within_budget": check_cost_within_budget(report, gate),
        "pairwise_a_not_worse": check_pairwise_a_not_worse(report, gate),
    }
    return {"passed": all(ok for ok, _ in checks.values()),
            "checks": {k: {"pass": ok, "detail": d} for k, (ok, d) in checks.items()}}
