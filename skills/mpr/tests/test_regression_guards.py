"""Regression guards (Spec 08 §6) — the two load-bearing hypotheses, as HARD gate conditions.

§6.1 router-decline on single-fact (deterministic, OFFLINE: the pre-check declines BEFORE any LLM call,
so a raising classifier is never reached → asserts both decline AND calls==0). §6.2 the A/B gate checks
(coverage ≥ baseline, cost ≤ budget, pairwise A not worse) as PURE functions over a synthetic judged
report + the tunable eval/gate.toml. The full eval-set calibration (≥8 queries/domain, real report) is
Ev-8/USER; this commits a STARTER negative set + the gate LOGIC that is deterministically green today.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from _router_fakes import FakeClassifierLLM, registry, run_panel

from mpr.router import classify
from mpr.schema import RouterInput

_EVAL = Path(__file__).resolve().parents[1] / "eval"
_PRECHECK_REASONS = {"single-fact lookup", "closed factual question",
                     "too short for multi-lens analysis", "single-source retrieval — no multi-perspective gain"}


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


G = _load("mpr_gate_probe", _EVAL / "gate.py")


def _single_fact_set():
    lines = (_EVAL / "sets" / "single_fact_decline.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(ln) for ln in lines if ln.strip()]


# ── §6.1 router-decline (offline, pre-check fires before any LLM call) ─────────────────────────────
def test_all_single_fact_queries_decline():
    decisions = []
    for row in _single_fact_set():
        llm = FakeClassifierLLM(raises=True)                 # if the pre-check is bypassed, this raises
        d = classify(RouterInput(query=row["query"]), llm=llm, registry=registry())
        decisions.append(d.decision.value)
        assert d.decision.value == "decline", row
        assert llm.calls == 0, f"{row['id']} reached the LLM (not a pre-check decline)"
        assert d.decline_reason in _PRECHECK_REASONS, (row, d.decline_reason)
    assert decisions and all(x == "decline" for x in decisions)


def test_decline_rate_above_threshold():
    gate = G.load_gate()
    decisions = []
    for row in _single_fact_set():
        d = classify(RouterInput(query=row["query"]), llm=FakeClassifierLLM(raises=True), registry=registry())
        decisions.append(d.decision.value)
    ok, rate = G.check_decline_rate(decisions, gate)
    assert ok and rate == 1.0                                # starter set is 100% pre-check decline


def test_run_queries_do_not_decline():
    # deliberative, multi-dimensional queries must NOT over-decline → pass the pre-check, classify to run.
    reply = run_panel(domain="architecture-decision", route="wide", mode="decision",
                      synthesis_template="decision-matrix", evidence_source="internal")
    run_qs = ["Sollten wir den Monolithen aufteilen? Bitte die Optionen vergleichen.",
              "Microservices versus Monolith — was ist die bessere Wahl?",
              "Welche Datenbank passt am besten zu hoher Schreiblast?"]
    for q in run_qs:
        d = classify(RouterInput(query=q), llm=FakeClassifierLLM(reply), registry=registry())
        assert d.decision.value == "run", q


# ── §6.2 A/B gate checks (pure, synthetic judged report) ──────────────────────────────────────────
def _report(a_cov, b_cov, pw, a_cost):
    return {f"q{i}": {"domain": "arch", "a": {"coverage": a_cov}, "b": {"coverage": b_cov},
                      "pairwise": {"coverage": pw}, "a_cost_usd": a_cost, "b_cost_usd": 0.0}
            for i in range(4)}


def test_gate_toml_loads_thresholds():
    gate = G.load_gate()
    assert gate["regression"]["decline_rate_min"] == 0.95
    assert {"coverage_floor", "coverage_epsilon", "pairwise_epsilon", "max_cost_usd_per_run"} <= set(gate["ab"])


def test_mpr_coverage_ge_baseline():
    gate = G.load_gate()
    assert G.check_coverage_ge_baseline(_report(4.0, 2.0, "a", 0.05), gate)[0] is True
    assert G.check_coverage_ge_baseline(_report(2.0, 4.0, "b", 0.05), gate)[0] is False   # A regressed below B
    assert G.check_coverage_ge_baseline(_report(2.5, 0.0, "a", 0.05), gate)[0] is False   # below floor 3.0


def test_cost_within_budget():
    gate = G.load_gate()
    assert G.check_cost_within_budget(_report(4.0, 2.0, "a", 0.10), gate)[0] is True       # 4×0.10 = 0.4 ≤ 2.0
    assert G.check_cost_within_budget(_report(4.0, 2.0, "a", 1.00), gate)[0] is False      # 4×1.00 = 4.0 > 2.0


def test_pairwise_a_not_worse():
    gate = G.load_gate()
    assert G.check_pairwise_a_not_worse(_report(4.0, 2.0, "a", 0.05), gate)[0] is True     # A wins all
    assert G.check_pairwise_a_not_worse(_report(2.0, 4.0, "b", 0.05), gate)[0] is False    # B wins all


def test_gate_report_aggregates_fail_fast():
    gate = G.load_gate()
    good = G.gate_report(_report(4.0, 2.0, "a", 0.05), ["decline"] * 8, gate)
    assert good["passed"] is True and all(c["pass"] for c in good["checks"].values())
    bad = G.gate_report(_report(2.0, 4.0, "b", 1.00), ["decline", "run"], gate)
    assert bad["passed"] is False
    assert bad["checks"]["coverage_ge_baseline"]["pass"] is False
    assert bad["checks"]["decline_rate"]["pass"] is False    # 1/2 = 0.5 < 0.95


def test_gate_fails_closed_on_empty_inputs():
    # a GATE must never green vacuously: no judged report (no coverage measured) + no decisions → fail.
    gate = G.load_gate()
    assert G.gate_report({}, [], gate)["passed"] is False
    assert G.check_coverage_ge_baseline({}, gate)[0] is False   # empty → fail-closed
    assert G.check_pairwise_a_not_worse({}, gate)[0] is False   # no pairwise signal → fail-closed
    assert G.check_decline_rate([], gate)[0] is False           # 0.0 < 0.95


# ── adversarial-review HIGH fixes: false-pass guards ──────────────────────────────────────────────
def test_coverage_fails_on_incomplete_report():
    # HIGH-1: a partial report (MPR covered only a subset) must NOT pass on the surviving queries.
    gate = G.load_gate()
    partial = _report(5.0, 1.0, "a", 0.05)        # 4 strong A queries…
    partial["q1"]["a"] = {}                        # …but one has NO MPR coverage (un-judged / dropped)
    ok, detail = G.check_coverage_ge_baseline(partial, gate)
    assert ok is False and detail["complete"] is False and detail["judged"] == 3 and detail["queries"] == 4


def test_pairwise_fails_closed_on_no_signal():
    # HIGH-2: a cost-only report (no judge scores) must not vacuously pass pairwise.
    gate = G.load_gate()
    cost_only = {"q0": {"a_cost_usd": 0.1, "b_cost_usd": 0.0, "pairwise": {}}}
    assert G.check_pairwise_a_not_worse(cost_only, gate)[0] is False


def test_merge_judged_report_is_the_producer_seam():
    # HIGH-2: the producer→gate merge is CODE + tested. harness.diff_arms(cost) + judge_panel(a/b/pairwise).
    gate = G.load_gate()
    diff = {"q1": {"domain": "arch", "a_cost_usd": 0.05, "b_cost_usd": 0.0},
            "q2": {"domain": "arch", "a_cost_usd": 0.04, "b_cost_usd": 0.0}}
    judgements = {"q1": {"a": {"coverage": 4.0}, "b": {"coverage": 2.0}, "pairwise": {"coverage": "a"}},
                  "q2": {"a": {"coverage": 5.0}, "b": {"coverage": 2.0}, "pairwise": {"coverage": "a"}}}
    report = G.merge_judged_report(diff, judgements)
    assert report["q1"]["a"]["coverage"] == 4.0 and report["q1"]["a_cost_usd"] == 0.05
    assert G.gate_report(report, ["decline"] * 8, gate)["passed"] is True
    # a query judged in diff but MISSING its judgement → incomplete → gate fails (no silent pass)
    report2 = G.merge_judged_report(diff, {"q1": judgements["q1"]})   # q2 un-judged
    assert G.check_coverage_ge_baseline(report2, gate)[0] is False
