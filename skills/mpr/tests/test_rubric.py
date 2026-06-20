"""Rubric scoring rules (Spec 08 §5.1) — pure, deterministic. Loaded standalone (eval/ is not a package)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

_RUBRIC = Path(__file__).resolve().parents[1] / "eval" / "rubric.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


R = _load("mpr_rubric_probe", _RUBRIC)


def test_weights_sum_to_one():
    assert abs(sum(d["weight"] for d in R.RUBRIC.values()) - 1.0) < 1e-9


def test_score_is_median_per_dimension():
    assert R.median_scores([{"coverage": 5}, {"coverage": 3}, {"coverage": 1}])["coverage"] == 3.0
    assert R.median_scores([{"coverage": 5}, {"coverage": 5}, {"coverage": 1}])["coverage"] == 5.0  # outlier-robust


def test_weighted_total_correct():
    full = {d: 4.0 for d in R.RUBRIC}
    assert R.weighted_total(full) == 4.0                       # uniform 4 over weights summing to 1 → 4.0
    # renormalised over present dims: only coverage(0.30) + grounding(0.20) present, scores 5 & 0
    assert R.weighted_total({"coverage": 5.0, "grounding": 0.0}) == round((0.30 * 5) / 0.50, 3)


def test_passes_enforces_floor():
    assert R.passes({"coverage": 4.0}, {"coverage": 3.0}) is True
    assert R.passes({"coverage": 2.0}, {"coverage": 3.0}) is False
    assert R.passes({}, {"coverage": 3.0}) is False           # fail-closed on a missing scored dim


def test_cost_latency_penalty():
    # high overhead + low value-add → heavily penalised; low overhead + high value-add → near full marks.
    bad = R.cost_latency_score(2.0, 60.0, value_add=1.0, cost_budget=0.5, latency_budget=30.0)
    good = R.cost_latency_score(0.1, 5.0, value_add=5.0, cost_budget=0.5, latency_budget=30.0)
    assert bad < good and bad <= 0.5 and good >= 4.0
    assert R.cost_latency_score(0.0, 0.0, value_add=0.0) == 5.0   # no overhead → full marks regardless of value


def test_score_combines_median_cost_latency_and_total():
    votes = [{d: 4 for d in R.JUDGED_DIMS}, {d: 2 for d in R.JUDGED_DIMS}]
    scored = R.score(votes, cost_latency=5.0)
    assert scored["coverage"] == 3.0 and scored["cost_latency"] == 5.0
    assert "total" in scored and 0.0 <= scored["total"] <= 5.0


# ── adversarial-review fixes ──────────────────────────────────────────────────────────────────────
def test_median_quorum_drops_single_judge_dim():
    # a dim scored by only ONE of two votes must NOT set the median at full weight (silent-bias fix).
    out = R.median_scores([{"coverage": 5, "grounding": 5}, {"coverage": 1}])
    assert out["coverage"] == 3.0 and "grounding" not in out          # grounding < quorum(2) → dropped
    assert R.median_scores([{"coverage": 5}])["coverage"] == 5.0      # n=1 → quorum 1 → single judge ok


def test_cost_latency_zero_budget_is_zero_tolerance():
    # budget 0 = zero tolerance (any overhead → 0), NOT 'penalty disabled'.
    assert R.cost_latency_score(5.0, 0.0, value_add=0.0, cost_budget=0) == 0.0
    assert R.cost_latency_score(0.0, 5.0, value_add=0.0, latency_budget=0) == 0.0
    assert R.cost_latency_score(5.0, 0.0, value_add=5.0, cost_budget=None) == 5.0   # None disables the axis


def test_passes_rejects_nonfinite():
    assert R.passes({"coverage": float("nan")}, {"coverage": 3.0}) is False   # NaN must not pass a floor
    assert R.passes({"coverage": float("inf")}, {"coverage": 3.0}) is False   # inf is not a real score
