"""P0-Wiring (PW-1) — run_mpr routes perspectives via the P0 ProviderDispatcher (Spec 08 §2.4 seam + §2.6
provenance). Net-free: an injected _StubDispatcher (mirrors active()+dispatch()) captures the RouteRequest[]
/DispatchPolicy MPR builds and returns canned DispatchResult rows; fanout is wired to RAISE so a test only
passes if the dispatch path is actually taken. These un-defer the §2.4/§2.6 tests the skip-stub tracked.
"""
from __future__ import annotations

import json
import types
from pathlib import Path

from _router_fakes import FakeClassifierLLM, registry, run_panel

from mpr.entry import Deps, run_mpr
from mpr.mpr_config import DEFAULT_POOL, DEFAULT_ROUTING

_DEC = json.dumps({
    "options": ["A", "B"], "criteria": [{"name": "K1", "weight": 2}, {"name": "K2", "weight": 3}],
    "cells": [{"option": "A", "criterion": "K1", "score": 4}, {"option": "A", "criterion": "K2", "score": 2},
              {"option": "B", "criterion": "K1", "score": 1}, {"option": "B", "criterion": "K2", "score": 5}],
    "recommendation": "B", "recommendation_rationale": "B überwiegt", "fallback": "A",
    "fallback_trigger": "wenn X", "conflict_notes": [],
})


def _writer(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _boom(*a, **k):                                   # fanout must NOT be reached when the dispatcher is used
    raise AssertionError("in-engine fanout reached — the dispatcher should have executed the panel")


def _val(x):
    return getattr(x, "value", x)


def _local_row(i):
    return {"ok": True, "content": f"Gutachten {i}", "error": None, "completion_tokens": 100, "latency": 0.1,
            "provider_id": "spark-vllm", "provider_kind": "in-engine", "model": "qwen3", "effort": "medium",
            "est_cost_usd": 0.0, "real_cost_usd": 0.0, "route_reason": "local-sovereign", "spilled": False}


def _offload_row(i):
    return {"ok": True, "content": f"Gutachten {i}", "error": None, "completion_tokens": 200, "latency": 0.2,
            "provider_id": "claude-sonnet", "provider_kind": "cli", "model": "claude-sonnet-4-6",
            "effort": "medium", "est_cost_usd": 0.015, "real_cost_usd": 0.02, "route_reason": "cost-fit",
            "spilled": False}


class _StubDispatcher:
    """Captures dispatch(items, contexts, policy) + returns one canned DispatchResult row per item."""

    def __init__(self, row):
        self.calls = []
        self._row = row

    def active(self):
        return True

    def dispatch(self, items, contexts=None, policy=None, **kw):
        self.calls.append(types.SimpleNamespace(items=list(items), contexts=contexts, policy=policy))
        return [dict(self._row(i)) for i in range(len(items))]


def _deps(tmp_path, llm, dispatcher, *, run_id="pw-0001", store=None, reducer=None, budget=None):
    return Deps(llm=llm, registry=registry(), dispatcher=dispatcher, fanout=_boom,
                synth_llm=lambda p, *, system, max_tokens: _DEC, reducer=reducer, store=store,
                writer=_writer, run_id=run_id, runs_dir=str(tmp_path), audit_level="full-per-perspective",
                pool=dict(DEFAULT_POOL), routing=dict(DEFAULT_ROUTING), default_offload="claude-sonnet",
                budget=budget,
                sovereignty={"default_policy": "offloadable", "internal_is_local_only": True, "fail_closed": True})


_ARCH = dict(domain="architecture-decision", route="wide", mode="decision",
             synthesis_template="decision-matrix", evidence_source="internal")
_COMP = dict(domain="competitive", route="wide", mode="comparison",
             synthesis_template="comparison-matrix", evidence_source="external")
_ARCH_Q = "Sollen wir von Modulith auf Microservices umstellen und die Optionen abwägen?"
_COMP_Q = "Vergleiche unser Pricing gegen die Top-Wettbewerber und zeige Stärken und Schwächen."


def _reqs(stub):
    assert len(stub.calls) == 1, "dispatch must be called exactly once"
    return stub.calls[0].policy.requests, stub.calls[0].policy


def _manifest(tmp_path, run_id):
    return json.loads((tmp_path / run_id / "manifest.json").read_text(encoding="utf-8"))


# ── the dispatch path is actually taken ───────────────────────────────────────────────────────────
def test_run_routes_through_dispatcher_not_fanout(tmp_path):
    stub = _StubDispatcher(_local_row)
    out = run_mpr(_ARCH_Q, deps=_deps(tmp_path, FakeClassifierLLM(run_panel(**_ARCH)), stub))
    assert out.startswith("<<<MPR_REPORT>>>") and len(stub.calls) == 1   # dispatched, fanout(_boom) untouched


# ── §2.4 RouteRequest seam (what MPR hands to P0) ─────────────────────────────────────────────────
def test_local_only_role_never_offloaded(tmp_path):
    stub = _StubDispatcher(_local_row)
    run_mpr(_ARCH_Q, deps=_deps(tmp_path, FakeClassifierLLM(run_panel(**_ARCH)), stub))
    reqs, _ = _reqs(stub)
    assert reqs and all(_val(r.provider_policy) == "local-only" for r in reqs)      # internal → local-only
    assert all(_val(r.sensitivity) == "sensitive" for r in reqs)                    # belt+suspenders local guard


def test_offloadable_role_allows_spill(tmp_path):
    stub = _StubDispatcher(_offload_row)
    run_mpr(_COMP_Q, deps=_deps(tmp_path, FakeClassifierLLM(run_panel(**_COMP)), stub))
    reqs, policy = _reqs(stub)
    assert reqs and all(_val(r.provider_policy) == "offloadable" for r in reqs)     # external → offloadable
    assert policy.allow_spill is True                                               # MPR does not block spill


def test_policy_passed_through_unaltered(tmp_path):
    for q, reply, pol, sens in ((_ARCH_Q, _ARCH, "local-only", "sensitive"),
                                (_COMP_Q, _COMP, "offloadable", "internal")):
        stub = _StubDispatcher(_local_row)
        run_mpr(q, deps=_deps(tmp_path, FakeClassifierLLM(run_panel(**reply)), stub, run_id=f"pw-{pol}"))
        reqs, _ = _reqs(stub)
        assert all(_val(r.provider_policy) == pol and _val(r.sensitivity) == sens for r in reqs)


def test_effort_forwarded_to_dispatch(tmp_path):
    stub = _StubDispatcher(_local_row)
    run_mpr(_ARCH_Q, deps=_deps(tmp_path, FakeClassifierLLM(run_panel(**_ARCH)), stub))
    reqs, _ = _reqs(stub)
    assert reqs and all(r.effort in {"low", "medium", "high", "xhigh"} for r in reqs)   # forwarded, not dropped


# ── §2.6 manifest provenance taken 1:1 from DispatchResult ────────────────────────────────────────
def test_manifest_provenance_from_offload_dispatch_result(tmp_path):
    stub = _StubDispatcher(_offload_row)
    run_mpr(_COMP_Q, deps=_deps(tmp_path, FakeClassifierLLM(run_panel(**_COMP)), stub, run_id="pw-off"))
    m = _manifest(tmp_path, "pw-off")
    ps = m["perspectives"]
    assert ps and all(p["provider"] == "claude-sonnet" and p["substrate"] == "pc-cli" for p in ps)
    assert all(p["model"] == "claude-sonnet-4-6" and p["route_reason"] == "cost-fit" for p in ps)
    assert all(p["cost"]["amount"] == 0.02 and p["spilled"] is False for p in ps)
    # real egress recorded + counted; offloadable → allowed (no violation)
    assert m["provenance"]["egress"] and m["sovereignty_summary"]["offloaded_count"] == len(ps)
    assert m["sovereignty_summary"]["external_egress_providers"] == ["claude-sonnet"]
    assert m["sovereignty_summary"]["violations"] == 0 and m["status"] == "ok"


def test_manifest_local_only_dispatch_stays_local_no_egress(tmp_path):
    # P0 keeps local-only lenses local → DispatchResult reports spark-vllm/in-engine → no egress in the proof.
    stub = _StubDispatcher(_local_row)
    run_mpr(_ARCH_Q, deps=_deps(tmp_path, FakeClassifierLLM(run_panel(**_ARCH)), stub, run_id="pw-loc"))
    m = _manifest(tmp_path, "pw-loc")
    assert all(p["substrate"] == "in-engine" and p["provider"] == "spark-vllm" for p in m["perspectives"])
    assert m["provenance"]["egress"] == [] and m["sovereignty_summary"]["offloaded_count"] == 0
    assert m["sovereignty_summary"]["violations"] == 0


def test_egress_carries_data_classification(tmp_path):
    # external/offloadable run → egress entries labelled 'public' (Sov-LOW-2: sensitivity threaded, not hard 'internal').
    stub = _StubDispatcher(_offload_row)
    run_mpr(_COMP_Q, deps=_deps(tmp_path, FakeClassifierLLM(run_panel(**_COMP)), stub, run_id="pw-dc"))
    egress = _manifest(tmp_path, "pw-dc")["provenance"]["egress"]
    assert egress and all(e["data_classification"] == "public" for e in egress)


def _spill_row(i):
    return {"ok": True, "content": f"G{i}", "error": None, "completion_tokens": 50, "latency": 0.1,
            "provider_id": "spark-fallback", "provider_kind": "in-engine", "model": None, "effort": "medium",
            "est_cost_usd": 0.0, "real_cost_usd": 0.0, "route_reason": "spill-fallback", "spilled": True}


def test_spill_back_to_local_is_not_a_violation(tmp_path):
    # Sov-LOW-3: an offloadable lens spilled back onto the local Spark (provider_id='spark-fallback',
    # in-engine) physically never egressed → recorded local, NO violation/egress (no over-flag).
    stub = _StubDispatcher(_spill_row)
    run_mpr(_COMP_Q, deps=_deps(tmp_path, FakeClassifierLLM(run_panel(**_COMP)), stub, run_id="pw-spill"))
    m = _manifest(tmp_path, "pw-spill")
    assert all(p["substrate"] == "in-engine" and p["spilled"] is True for p in m["perspectives"])
    assert m["provenance"]["egress"] == [] and m["sovereignty_summary"]["violations"] == 0
    assert m["status"] == "ok"


def test_classifier_adapter_disables_thinking():
    # live-gap fix: the classify call must disable qwen3 reasoning (structured emission), else <think>
    # burns the small ROUTER_MAX_TOKENS cap → empty content → router-classify-failed on the real model.
    from mpr.entry import _ClassifierAdapter
    captured = {}

    def _create(**kw):
        captured.update(kw)
        msg = types.SimpleNamespace(content='{"decision":"decline","decline_reason":"x"}')
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])

    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=_create)))
    out = _ClassifierAdapter(client, "m").complete_json("sys", "usr", max_tokens=768, temperature=0.2)
    assert '"decision"' in out
    assert captured["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False


class _BadDispatcher:
    def active(self):
        return True

    def dispatch(self, items, contexts=None, policy=None, **kw):
        return ["not-a-dict" for _ in items]            # a buggy/hostile dispatcher


def test_non_dict_dispatch_row_degrades_cleanly(tmp_path):
    # Rob-LOW-3: malformed rows must degrade at the dispatch stage (ok=false), never crash the synthesis loop.
    out = run_mpr(_ARCH_Q, deps=_deps(tmp_path, FakeClassifierLLM(run_panel(**_ARCH)), _BadDispatcher(), run_id="pw-bad"))
    assert not out.startswith("ERROR: mpr_research: synthesis")     # not mis-attributed / not a crash
    m = _manifest(tmp_path, "pw-bad")
    assert m["perspectives"] and all(p["ok"] is False and p["error"] == "malformed execution result"
                                     for p in m["perspectives"])


# ── MPR-1 (#503): the per-run cost/token budget is enforced + recorded ─────────────────────────────
def test_run_budget_cap_passed_to_dispatch_policy(tmp_path):
    # The cfg budget cap reaches the dispatcher's router as a usd_cap. This is the correct MPR-layer
    # assertion (propagation): the router's actual admission gate — dropping unaffordable candidates to
    # `budget-exhausted` — is the engine's contract, covered by ack/tests/test_router.py
    # ::test_budget_gate_falls_to_cheap_local_then_exhausted. Pre-fix DispatchPolicy got no budget at all.
    from mpr.mpr_config import RunBudget
    stub = _StubDispatcher(_local_row)
    run_mpr(_ARCH_Q, deps=_deps(tmp_path, FakeClassifierLLM(run_panel(**_ARCH)), stub, run_id="pw-cap",
                                budget=RunBudget(max_cost_usd=3.5, max_tokens=50000)))
    _, policy = _reqs(stub)
    assert policy.budget is not None and policy.budget.usd_cap == 3.5


def test_manifest_records_real_budget_spend(tmp_path):
    # Each perspective's REAL cost/tokens are charged to the run budget and snapshotted into the manifest
    # (pre-fix: cfg.budget was parsed but never bound → manifest.budget stayed null, the cap was dead).
    from mpr.mpr_config import RunBudget
    stub = _StubDispatcher(_offload_row)
    run_mpr(_COMP_Q, deps=_deps(tmp_path, FakeClassifierLLM(run_panel(**_COMP)), stub, run_id="pw-bud",
                                budget=RunBudget(max_cost_usd=10.0, max_tokens=100000)))
    m = _manifest(tmp_path, "pw-bud")
    n = len(m["perspectives"])
    assert n >= 1 and m["budget"] is not None
    assert m["budget"]["max_cost_usd_per_run"] == 10.0 and m["budget"]["max_tokens_per_run"] == 100000
    assert m["budget"]["spent_cost_usd"] == round(0.02 * n, 6) and m["budget"]["spent_tokens"] == 200 * n
    pp = m["budget"]["per_provider_spent"]["claude-sonnet"]
    assert pp["cost_usd"] == round(0.02 * n, 6) and pp["tokens"] == 200 * n


def test_no_budget_leaves_run_unbounded(tmp_path):
    # Backwards-compat: with no budget configured the manifest carries no budget block and the dispatch
    # policy stays unbounded (the dispatcher falls back to its own default Budget()).
    stub = _StubDispatcher(_local_row)
    run_mpr(_ARCH_Q, deps=_deps(tmp_path, FakeClassifierLLM(run_panel(**_ARCH)), stub, run_id="pw-nob"))
    assert _manifest(tmp_path, "pw-nob").get("budget") is None
    _, policy = _reqs(stub)
    assert policy.budget is None


def test_token_cap_clamps_in_engine_fanout(tmp_path):
    # The DEFAULT (in-engine fanout) lane has no router usd_cap to gate it, so the run token cap binds by
    # clamping the per-lens budget: n lenses × per-lens ≤ max_tokens_per_run. Pre-fix the panel ran at the
    # full per-lens budget regardless of the configured cap.
    from mpr.mpr_config import RunBudget
    seen = {}

    def _cap_fanout(prompts, *, system, max_tokens, think):
        seen["max_tokens"], seen["n"] = max_tokens, len(prompts)
        return [{"ok": True, "content": f"G{i}", "error": None, "completion_tokens": max_tokens,
                 "latency": 0.1} for i in range(len(prompts))]

    deps = _deps(tmp_path, FakeClassifierLLM(run_panel(**_ARCH)), None, run_id="pw-tok",   # no dispatcher → fanout
                 budget=RunBudget(max_cost_usd=10.0, max_tokens=300))
    deps.fanout = _cap_fanout                                  # capture the budget the fanout lane runs with
    run_mpr(_ARCH_Q, deps=deps)
    assert seen["max_tokens"] == max(1, 300 // seen["n"])      # per-lens clamped to the run cap
    assert _manifest(tmp_path, "pw-tok")["budget"]["spent_tokens"] <= 300   # whole panel stays within cap


def test_token_cap_degenerate_caps_stay_bounded(tmp_path):
    # A sub-lens-count cap (cap < n) and a non-positive cap must NOT fall back to the full per-lens budget
    # (the pre-fix unbounded run). A model call needs >=1 token, so each lens is floored to 1 → the panel is
    # bounded to n tokens (= max(cap, n)), never the default per-lens budget.
    from mpr.mpr_config import RunBudget
    for cap, run_id in ((1, "pw-tiny"), (0, "pw-zero")):
        seen = {}

        def _cap_fanout(prompts, *, system, max_tokens, think):
            seen["max_tokens"], seen["n"] = max_tokens, len(prompts)
            return [{"ok": True, "content": f"G{i}", "error": None, "completion_tokens": max_tokens,
                     "latency": 0.1} for i in range(len(prompts))]

        deps = _deps(tmp_path, FakeClassifierLLM(run_panel(**_ARCH)), None, run_id=run_id,
                     budget=RunBudget(max_cost_usd=10.0, max_tokens=cap))
        deps.fanout = _cap_fanout
        run_mpr(_ARCH_Q, deps=deps)
        assert seen["max_tokens"] == 1                          # floored to 1, not the ~_PANEL_DIRECT_TOKENS default
        spent = _manifest(tmp_path, run_id)["budget"]["spent_tokens"]
        assert spent == seen["n"] <= max(cap, seen["n"])        # bounded to n — never unbounded
