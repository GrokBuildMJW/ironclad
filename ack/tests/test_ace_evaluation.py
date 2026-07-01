"""ACE-EVAL (#855 / #865): the shipped metrics surface + the comparative-baseline cost harness. Pins
I-001/I-002 (Task/Scenario Goal Completion + per-difficulty), I-003 (exact-match accuracy), J-001 (no
full-rewrite / non-LLM merge), J-002 (>50% rollout reduction vs evolutionary), J-003 (KV-cache stable
prefix), L-002 (adaptation epochs).
"""
from __future__ import annotations

import json

from ack.ace import (Outcome, accuracy, goal_completion, compare_adaptation, ace_adapt,
                     full_rewrite_adapt, evolutionary_adapt, rollout_reduction, kv_cache_metrics,
                     validate_epochs, EvalReport, AdaptationCost, Sample, OfflineConfig,
                     DEFAULT_MAX_EPOCHS)


def _chat():
    payload = json.dumps({"insights": [{"content": "lesson", "section": "apis_to_use"}], "ratings": []})
    return lambda prompt: payload


# ─── I-003 accuracy ──────────────────────────────────────────────────────────────────────────────────
def test_accuracy_is_exact_match_fraction():
    outs = [Outcome(predicted="42", ground_truth="42"),
            Outcome(predicted="hi ", ground_truth=" hi"),          # trimmed match
            Outcome(predicted="x", ground_truth="y"),
            Outcome(success=True)]                                  # no ground truth → ignored
    assert accuracy(outs) == 2 / 3


def test_accuracy_empty_is_zero():
    assert accuracy([]) == 0.0 and accuracy([Outcome(success=True)]) == 0.0


# ─── I-001 / I-002 goal completion (+ per difficulty) ────────────────────────────────────────────────
def test_goal_completion_overall_and_per_difficulty():
    outs = [Outcome(success=True, difficulty="easy"), Outcome(success=False, difficulty="easy"),
            Outcome(success=True, difficulty="hard")]
    gc = goal_completion(outs, by_difficulty=True)
    assert gc["overall"] == 2 / 3 and gc["n"] == 3 and gc["passed"] == 2
    assert gc["by_difficulty"]["easy"]["overall"] == 0.5
    assert gc["by_difficulty"]["hard"]["overall"] == 1.0


def test_goal_completion_without_breakdown():
    gc = goal_completion([Outcome(success=True), Outcome(success=True)])
    assert gc["overall"] == 1.0 and "by_difficulty" not in gc


# ─── J-001 / J-002 comparative-baseline harness ──────────────────────────────────────────────────────
def test_ace_does_no_full_rewrite_and_no_llm_merge():
    cost = ace_adapt([Sample(query="q1"), Sample(query="q2")], chat=_chat())
    assert isinstance(cost, AdaptationCost)
    assert cost.full_rewrites == 0 and cost.llm_merges == 0     # J-001: local deltas + deterministic merge
    assert cost.rollouts == 2                                   # one reflect rollout per sample (1 epoch)


def test_full_rewrite_baseline_rewrites_per_sample():
    cost = full_rewrite_adapt([Sample(query="q1"), Sample(query="q2"), Sample(query="q3")], chat=_chat())
    assert cost.rollouts == 3 and cost.full_rewrites == 3 and cost.llm_merges == 3


def test_evolutionary_baseline_runs_a_validation_loop():
    cost = evolutionary_adapt([Sample(query="q1"), Sample(query="q2")], chat=_chat(), population=8)
    assert cost.rollouts == 16                                  # 2 samples x population 8


def test_compare_adaptation_meets_the_efficiency_claims():
    samples = [Sample(query=f"q{i}") for i in range(5)]
    rep = compare_adaptation(samples, chat=_chat(), population=8, config=OfflineConfig(max_epochs=1))
    assert rep["no_full_rewrite"] is True                       # J-001
    assert rep["rollout_target_met"] is True                    # J-002: >50% vs evolutionary
    assert rep["rollout_reduction_vs_evolutionary"] > 0.5
    assert rep["ace"].rollouts == 5 and rep["evolutionary"].rollouts == 40


def test_rollout_reduction_is_zero_on_empty_baseline():
    assert rollout_reduction(AdaptationCost("ace", rollouts=3), AdaptationCost("x", rollouts=0)) == 0.0


# ─── J-003 KV-cache stable prefix ────────────────────────────────────────────────────────────────────
def test_kv_cache_ratio_high_for_append_only_renders():
    # a realistic (large, stable) playbook prefix with a small per-step append → most of the prompt is cacheable
    base = "=== PLAYBOOK (ACE) BEGIN ===\n" + "".join(f"- [b-{i}] established strategy number {i}\n"
                                                       for i in range(12))
    renders = [base, base + "- [b-12] a new rule\n", base + "- [b-12] a new rule\n- [b-13] another\n"]
    m = kv_cache_metrics(renders)
    assert m["steps"] == 2 and m["cacheable_ratio"] > 0.8       # append-only → most of the prompt is cacheable


def test_kv_cache_low_when_prefix_changes():
    m = kv_cache_metrics(["alpha context block", "totally different block"])
    assert m["cacheable_ratio"] < 0.3                           # a rewrite invalidates the cache
    assert kv_cache_metrics(["only one"])["cacheable_ratio"] == 1.0


# ─── L-002 epochs ────────────────────────────────────────────────────────────────────────────────────
def test_validate_epochs_floor_and_default():
    assert validate_epochs(3) == 3 and validate_epochs(0) == 1 and validate_epochs(-5) == 1
    assert validate_epochs(None) == DEFAULT_MAX_EPOCHS == 5
    assert validate_epochs("bad") == 5


def test_eval_report_serializes():
    r = EvalReport(accuracy=0.9, task_goal_completion={"overall": 0.8}, max_epochs=5)
    d = r.to_dict()
    assert d["accuracy"] == 0.9 and d["task_goal_completion"]["overall"] == 0.8 and d["max_epochs"] == 5
