"""Provider dispatch (engine/dispatch.py) — per-substrate governor + budget ledger (P0-3).

Pure primitives only (the full ProviderDispatcher orchestration lands in P0-4): the CLI-pool
concurrency cap and the running budget ledger. The Spark envelope is reused unchanged from
ReasoningWorkers and is not retested here.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

from dispatch import EFFORT_MAX_TOKENS, BudgetLedger, plan_pool_concurrency  # noqa: E402


def test_effort_mapping_is_the_router_ssot():
    # Re-exported, not redefined — same object/values as the router SSOT.
    from router import EFFORT_MAX_TOKENS as ROUTER_MAP
    assert EFFORT_MAX_TOKENS is ROUTER_MAP
    assert EFFORT_MAX_TOKENS == {"low": 512, "medium": 1024, "high": 2048, "xhigh": 4096}


def test_plan_pool_concurrency_is_the_min():
    assert plan_pool_concurrency(n=10, max_agents=3, provider_max_concurrent=4) == 3   # client cap binds
    assert plan_pool_concurrency(n=2, max_agents=3, provider_max_concurrent=4) == 2    # n binds
    assert plan_pool_concurrency(n=10, max_agents=8, provider_max_concurrent=2) == 2   # provider cap binds


def test_plan_pool_concurrency_floor_one():
    assert plan_pool_concurrency(n=0, max_agents=3, provider_max_concurrent=4) == 1    # always ≥ 1
    assert plan_pool_concurrency(n=5, max_agents=0, provider_max_concurrent=0) == 1


def test_budget_ledger_charge_and_afford():
    led = BudgetLedger()
    assert led.spent == 0.0
    assert led.can_afford(0.05, cap=None) is True          # no cap → always affordable
    assert led.can_afford(0.05, cap=0.10) is True
    led.charge(0.08)
    assert led.spent == 0.08
    assert led.can_afford(0.05, cap=0.10) is False          # 0.08 + 0.05 > 0.10
    assert led.can_afford(0.02, cap=0.10) is True


def test_budget_ledger_reconcile_estimate_to_actual():
    led = BudgetLedger()
    led.charge(0.05)                # charged the estimate up front
    led.reconcile(estimate=0.05, actual=0.03)  # real cost came in lower
    assert abs(led.spent - 0.03) < 1e-9
    led.reconcile(estimate=0.0, actual=0.10)   # an extra real cost
    assert abs(led.spent - 0.13) < 1e-9


def test_budget_ledger_never_negative():
    led = BudgetLedger()
    led.charge(0.02)
    led.reconcile(estimate=0.05, actual=0.0)   # over-estimate → clamps at 0, not negative
    assert led.spent == 0.0


# ── ProviderDispatcher (P0-4): fake substrates, no subprocess/no model ────────────────────────────
import pytest  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from dispatch import PROVENANCE_FIELDS, DispatchPolicy, ProviderDispatcher  # noqa: E402
from providers import load_registry  # noqa: E402
from router import Budget, LoadSignal, ProviderPolicy, RouteRequest, Sensitivity  # noqa: E402

SPARK = {"provider_id": "spark-vllm", "kind": "in-engine", "model": "qwen3.6-35b",
         "endpoint_env": "GX10_BASE_URL", "capabilities": {"local": True, "max_effort": "xhigh"}}
SONNET = {"provider_id": "claude-sonnet", "kind": "cli", "model": "sonnet", "bin": "claude",
          "cmd_template": "{bin} --model {model} --print {prompt}",
          "cost_per_1k_in": 0.003, "cost_per_1k_out": 0.015, "capabilities": {"max_effort": "xhigh"}}

REG = load_registry({"providers": {"pool": [SPARK, SONNET]}})
REG_NOLOCAL = load_registry({"providers": {"pool": [SONNET]}})
BUSY = LoadSignal(spark_chat_busy=True)


class FakeWorkers:
    def __init__(self, fail_substr=None):
        self.calls = []
        self.fail_substr = fail_substr

    def fanout(self, prompts, *, system=None, contexts=None, max_tokens=None, temperature=0.7, think=True):
        self.calls.append({"prompts": list(prompts), "max_tokens": max_tokens})
        out = []
        for p in prompts:
            if self.fail_substr and self.fail_substr in p:
                out.append({"ok": False, "content": None, "error": "boom", "completion_tokens": None, "latency": 0.0})
            else:
                out.append({"ok": True, "content": f"spark:{p}", "error": None, "completion_tokens": 7, "latency": 0.0})
        return out


def make_runner(fail=False, raise_exc=False):
    def runner(spec, prompt, *, effort, max_tokens=None):
        if raise_exc:
            raise RuntimeError("cli boom")
        if fail:
            return {"ok": False, "content": None, "error": "exit1", "completion_tokens": None, "latency": 0.0}
        return {"ok": True, "content": f"cli:{spec.provider_id}:{prompt}", "error": None,
                "completion_tokens": None, "latency": 0.0}
    return runner


def _pol(n, *, sensitivity=Sensitivity.PUBLIC, policy=ProviderPolicy.OFFLOADABLE, load=None, allow_spill=True, effort="medium"):
    reqs = [RouteRequest(index=i, sensitivity=sensitivity, provider_policy=policy, effort=effort) for i in range(n)]
    return DispatchPolicy(reqs, load=load, allow_spill=allow_spill)


def test_inactive_delegates_byte_identical():
    disp = ProviderDispatcher(None, workers=FakeWorkers(), enabled=False)  # registry None → inactive
    res = disp.dispatch(["a", "b"])
    assert [r["content"] for r in res] == ["spark:a", "spark:b"]
    assert "provider_id" not in res[0]                   # no provenance keys → byte-identical passthrough
    # enabled but empty pool is also inactive
    disp2 = ProviderDispatcher(load_registry({"providers": {"pool": []}}), workers=FakeWorkers(), enabled=True)
    assert disp2.active() is False


def test_active_idle_routes_all_local_in_order():
    disp = ProviderDispatcher(REG, workers=FakeWorkers(), agent_runner=make_runner(), enabled=True)
    res = disp.dispatch(["a", "b", "c"])  # default reqs medium/internal, idle → local
    assert [r["provider_id"] for r in res] == ["spark-vllm"] * 3
    assert [r["content"] for r in res] == ["spark:a", "spark:b", "spark:c"]  # input order preserved


def test_spill_routes_cli_in_order():
    wk = FakeWorkers()
    disp = ProviderDispatcher(REG, workers=wk, agent_runner=make_runner(), enabled=True)
    res = disp.dispatch(["a", "b"], policy=_pol(2, load=BUSY))  # PUBLIC + chat-busy → spill to sonnet
    assert [r["provider_id"] for r in res] == ["claude-sonnet", "claude-sonnet"]
    assert [r["content"] for r in res] == ["cli:claude-sonnet:a", "cli:claude-sonnet:b"]
    assert wk.calls == []                                # Spark untouched when everything spilled


def test_sovereignty_sensitive_stays_local_even_busy():
    runner = make_runner()
    disp = ProviderDispatcher(REG, workers=FakeWorkers(), agent_runner=runner, enabled=True)
    res = disp.dispatch(["x"], policy=_pol(1, sensitivity=Sensitivity.SENSITIVE, load=BUSY))
    assert res[0]["provider_id"] == "spark-vllm"          # forced local despite chat-busy
    assert res[0]["content"] == "spark:x"


def test_cli_failure_spills_to_local():
    disp = ProviderDispatcher(REG, workers=FakeWorkers(), agent_runner=make_runner(fail=True), enabled=True)
    res = disp.dispatch(["a"], policy=_pol(1, load=BUSY))  # → sonnet (cli) fails → spill to spark
    assert res[0]["ok"] is True
    assert res[0]["spilled"] is True
    assert res[0]["route_reason"] == "spill-fallback"
    assert res[0]["content"] == "spark:a"


def test_runner_exception_isolated_no_throw():
    disp = ProviderDispatcher(REG, workers=FakeWorkers(), agent_runner=make_runner(raise_exc=True), enabled=True)
    res = disp.dispatch(["a", "b"], policy=_pol(2, load=BUSY, allow_spill=False))  # no rescue → stays failed
    assert len(res) == 2
    assert all(r["ok"] is False and "cli boom" in (r["error"] or "") for r in res)


def test_unroutable_when_sensitive_and_no_local():
    disp = ProviderDispatcher(REG_NOLOCAL, workers=FakeWorkers(), agent_runner=make_runner(), enabled=True)
    res = disp.dispatch(["a"], policy=_pol(1, sensitivity=Sensitivity.SENSITIVE))
    assert res[0]["ok"] is False
    assert "unroutable: no-local-provider" in res[0]["error"]


def test_no_cli_runner_fails_soft():
    disp = ProviderDispatcher(REG, workers=FakeWorkers(), agent_runner=None, enabled=True)
    res = disp.dispatch(["a"], policy=_pol(1, load=BUSY, allow_spill=False))  # cli routed, no runner
    assert res[0]["ok"] is False
    assert "no-cli-runner" in res[0]["error"]


def test_spark_batch_max_tokens_is_the_max_est():
    wk = FakeWorkers()
    disp = ProviderDispatcher(REG, workers=wk, agent_runner=make_runner(), enabled=True)
    pol = DispatchPolicy([RouteRequest(index=0, effort="low"), RouteRequest(index=1, effort="high")])
    disp.dispatch(["a", "b"], policy=pol)  # idle → both local → one fanout batch
    assert wk.calls[0]["max_tokens"] == 2048             # max(low=512, high=2048)


REMOTE_INENGINE = {"provider_id": "remote-api", "kind": "in-engine", "model": "m",
                   "endpoint_env": "X", "capabilities": {"local": False, "max_effort": "xhigh"}}


def test_non_local_in_engine_is_unsupported_failsoft():
    reg = load_registry({"providers": {"pool": [REMOTE_INENGINE]}})  # no local, no remote runner in P0
    disp = ProviderDispatcher(reg, workers=None, agent_runner=make_runner(), enabled=True)
    res = disp.dispatch(["a"])
    assert res[0]["ok"] is False
    assert "unsupported-substrate" in res[0]["error"]   # provenance honest, no silent local run


def test_workers_none_local_routed_failsoft_no_throw():
    disp = ProviderDispatcher(REG, workers=None, agent_runner=make_runner(), enabled=True)
    res = disp.dispatch(["a"])  # idle → local routed, but no Spark substrate
    assert res[0]["ok"] is False
    assert "no-spark-substrate" in res[0]["error"]


def test_malformed_completion_tokens_does_not_throw():
    def bad_runner(spec, prompt, *, effort, max_tokens=None):
        return {"ok": True, "content": "x", "error": None, "completion_tokens": "abc", "latency": 0.0}
    disp = ProviderDispatcher(REG, workers=FakeWorkers(), agent_runner=bad_runner, enabled=True)
    res = disp.dispatch(["a"], policy=_pol(1, load=BUSY))  # → cli; _real_cost must not raise on "abc"
    assert res[0]["ok"] is True and res[0]["content"] == "x"


def test_no_capable_provider_is_not_spilled():
    reg_weak = load_registry({"providers": {"pool": [
        {**SPARK, "capabilities": {"local": True, "max_effort": "medium"}}]}})
    disp = ProviderDispatcher(reg_weak, workers=FakeWorkers(), agent_runner=make_runner(), enabled=True)
    res = disp.dispatch(["a"], policy=_pol(1, effort="xhigh"))  # effort ceiling → no-capable-provider
    assert res[0]["ok"] is False
    assert "no-capable-provider" in res[0]["error"]
    assert res[0]["spilled"] is False   # §5.3-3: unroutable stays unroutable, no capability-blind spill


def test_envelope_split_partitions_local_and_cli(monkeypatch):
    # §9: N local items go to the Spark fanout sub-batch; external items go to the CLI runner.
    wk = FakeWorkers()
    disp = ProviderDispatcher(REG, workers=wk, agent_runner=make_runner(), enabled=True)
    reqs = [RouteRequest(index=0, sensitivity=Sensitivity.SENSITIVE),   # forced local → Spark
            RouteRequest(index=1, sensitivity=Sensitivity.PUBLIC)]       # busy → spill to CLI
    res = disp.dispatch(["s", "c"], policy=DispatchPolicy(reqs, load=BUSY))
    assert len(wk.calls) == 1 and wk.calls[0]["prompts"] == ["s"]   # Spark sub-batch = only the local item
    assert wk.calls[0]["max_tokens"] == 1024                        # max of est_max_tokens (medium)
    assert res[0]["provider_id"] == "spark-vllm" and res[0]["content"] == "spark:s"
    assert res[1]["provider_id"] == "claude-sonnet" and res[1]["content"] == "cli:claude-sonnet:c"


def test_budget_accounting_accumulates_across_items():
    # §9: spent accumulates per item; once the cap is hit, later items fall to the cheap local provider.
    disp = ProviderDispatcher(REG, workers=FakeWorkers(), agent_runner=make_runner(), enabled=True)
    pol = DispatchPolicy(
        [RouteRequest(index=0, sensitivity=Sensitivity.PUBLIC),
         RouteRequest(index=1, sensitivity=Sensitivity.PUBLIC)],
        load=BUSY, budget=Budget(usd_cap=0.02),         # fits one Sonnet call (~0.0154), not two
    )
    res = disp.dispatch(["a", "b"], policy=pol)
    assert res[0]["provider_id"] == "claude-sonnet"     # first item within budget → external
    assert res[1]["provider_id"] == "spark-vllm"        # second: cap hit → spill to cheap local
    assert res[1]["route_reason"] == "spill-budget"


def test_provenance_fields_complete_on_every_active_result():
    # §8 audit contract: every active-path result (spark / cli / unroutable / spilled) carries the
    # full PROVENANCE_FIELDS set so the MPR manifest can prove sovereignty per perspective.
    spark = ProviderDispatcher(REG, workers=FakeWorkers(), agent_runner=make_runner(), enabled=True).dispatch(["a"])[0]
    cli = ProviderDispatcher(REG, workers=FakeWorkers(), agent_runner=make_runner(), enabled=True).dispatch(
        ["b"], policy=_pol(1, load=BUSY))[0]
    unroutable = ProviderDispatcher(REG_NOLOCAL, workers=FakeWorkers(), agent_runner=make_runner(), enabled=True).dispatch(
        ["c"], policy=_pol(1, sensitivity=Sensitivity.SENSITIVE))[0]
    spilled = ProviderDispatcher(REG, workers=FakeWorkers(), agent_runner=make_runner(fail=True), enabled=True).dispatch(
        ["d"], policy=_pol(1, load=BUSY))[0]
    for label, r in [("spark", spark), ("cli", cli), ("unroutable", unroutable), ("spilled", spilled)]:
        for f in PROVENANCE_FIELDS:
            assert f in r, f"{f} missing on {label} result"
    assert spark["provider_kind"] == "in-engine"     # provenance is honest, not a lie
    assert cli["provider_kind"] == "cli"
    assert spilled["spilled"] is True


def test_default_cli_runner_renders_argv(monkeypatch):
    import client  # noqa: E402
    captured = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv
        return SimpleNamespace(returncode=0, stdout="answer", stderr="")

    monkeypatch.setattr(client.subprocess, "run", fake_run)
    spec = SimpleNamespace(cmd_template="{bin} --model {model} --print {prompt}",
                           bin="claude", model="sonnet", permission_mode=None)
    r = client.default_cli_runner(spec, "hello world", effort="high")
    assert r["ok"] is True and r["content"] == "answer"
    assert captured["argv"][:4] == ["claude", "--model", "sonnet", "--print"]
    assert "hello world" in captured["argv"]              # prompt stays one argument

