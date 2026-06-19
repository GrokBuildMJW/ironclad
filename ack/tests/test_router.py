"""Provider routing-policy engine (engine/router.py) — pure, deterministic.

Covers the three axes + the hard sovereignty guarantee: SENSITIVE / local-only never leave a local
provider (or decline, never silent offload); idle prefers local; chat-busy / batch-full spill to
externals; capability + effort-ceiling filtering; cost/effort tiering (medium→Sonnet, xhigh→cheapest
capable); budget gate; and snapshot determinism.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

from providers import load_registry  # noqa: E402
from router import (  # noqa: E402
    Budget,
    EFFORT_MAX_TOKENS,
    LoadSignal,
    ProviderPolicy,
    RouteRequest,
    Sensitivity,
    route_one,
)

SPARK = {"provider_id": "spark-vllm", "kind": "in-engine", "model": "qwen3.6-35b",
         "endpoint_env": "GX10_BASE_URL", "capabilities": {"local": True, "max_effort": "xhigh"}}
SONNET = {"provider_id": "claude-sonnet", "kind": "cli", "model": "sonnet", "bin": "claude",
          "cmd_template": "{bin} --model {model} --print {prompt}",
          "cost_per_1k_in": 0.003, "cost_per_1k_out": 0.015, "capabilities": {"max_effort": "high"}}
OPUS = {"provider_id": "claude-opus", "kind": "cli", "model": "opus", "bin": "claude",
        "cmd_template": "{bin} --model {model} --print {prompt}", "weight": 90,
        "cost_per_1k_in": 0.015, "cost_per_1k_out": 0.075, "capabilities": {"max_effort": "xhigh"}}
KIMI = {"provider_id": "kimi-cli", "kind": "cli", "model": "k2", "bin": "kimi",
        "cmd_template": "{bin} --model {model} {prompt}", "weight": 80,
        "cost_per_1k_in": 0.001, "cost_per_1k_out": 0.002,
        "capabilities": {"max_effort": "xhigh", "web_search": True}}

REG = load_registry({"providers": {"pool": [SPARK, SONNET, OPUS, KIMI], "default_id": "spark-vllm"}})
REG_NOLOCAL = load_registry({"providers": {"pool": [SONNET, OPUS, KIMI]}})
IDLE = LoadSignal()
BUSY = LoadSignal(spark_chat_busy=True)
FULL = LoadSignal(spark_inflight=8, spark_batch_width=8)
NOBUDGET = Budget()


def _req(**kw):
    kw.setdefault("index", 0)
    kw.setdefault("sensitivity", Sensitivity.PUBLIC)
    return RouteRequest(**kw)


def test_idle_prefers_local():
    d = route_one(_req(effort="medium"), REG, IDLE, NOBUDGET)
    assert d.provider_id == "spark-vllm"
    assert d.reason == "local-sovereign"


def test_sensitive_forces_local_even_when_busy():
    d = route_one(_req(sensitivity=Sensitivity.SENSITIVE), REG, BUSY, NOBUDGET)  # busy would spill, but sovereignty wins
    assert d.provider_id == "spark-vllm"
    d2 = route_one(_req(sensitivity=Sensitivity.SENSITIVE), REG_NOLOCAL, IDLE, NOBUDGET)
    assert d2.provider_id is None and d2.reason == "no-local-provider"


def test_local_only_policy_forces_local():
    d = route_one(_req(provider_policy=ProviderPolicy.LOCAL_ONLY), REG, BUSY, NOBUDGET)
    assert d.provider_id == "spark-vllm"


def test_sovereignty_assertion_holds_across_states():
    # The load-bearing guarantee: a non-None decision under SENSITIVE/local-only is always local.
    cases = [
        _req(sensitivity=Sensitivity.SENSITIVE),
        _req(sensitivity=Sensitivity.SENSITIVE, needs_web=True),
        _req(provider_policy=ProviderPolicy.LOCAL_ONLY, effort="xhigh"),
    ]
    for req in cases:
        for load in (IDLE, BUSY, FULL):
            d = route_one(req, REG, load, NOBUDGET)
            if d.provider_id is not None:
                assert REG.by_id()[d.provider_id].capabilities.local is True


def test_spill_on_chat_busy_picks_external():
    d = route_one(_req(effort="medium"), REG, BUSY, NOBUDGET)
    assert d.provider_id == "claude-sonnet"  # medium tier → Sonnet (best effort-fit/cost)
    assert d.reason == "spill-load"
    assert REG.by_id()[d.provider_id].capabilities.local is False


def test_spill_on_batch_full():
    d = route_one(_req(effort="medium"), REG, FULL, NOBUDGET)
    assert d.provider_id != "spark-vllm"
    assert REG.by_id()[d.provider_id].capabilities.local is False


def test_capability_web_required_filters_non_web():
    # needs_web filters out spark (no web) even when idle → only kimi qualifies.
    d = route_one(_req(needs_web=True), REG, IDLE, NOBUDGET)
    assert d.provider_id == "kimi-cli"


def test_effort_ceiling_filters_too_weak_then_cheapest_wins():
    # xhigh: Sonnet (max_effort=high) is filtered; among xhigh externals the cheaper Kimi wins.
    d = route_one(_req(effort="xhigh"), REG, BUSY, NOBUDGET)
    assert d.provider_id != "claude-sonnet"
    assert d.provider_id == "kimi-cli"


def test_budget_gate_falls_to_cheap_local_then_exhausted():
    tiny = Budget(usd_cap=0.0001)
    d = route_one(_req(effort="high"), REG, BUSY, tiny)            # no external affordable → cheap local
    assert d.provider_id == "spark-vllm" and d.reason == "spill-budget"
    d2 = route_one(_req(effort="high"), REG_NOLOCAL, BUSY, tiny)   # no local either → exhausted
    assert d2.provider_id is None and d2.reason == "budget-exhausted"


def test_est_max_tokens_from_mapping():
    assert route_one(_req(effort="high"), REG, IDLE, NOBUDGET).est_max_tokens == EFFORT_MAX_TOKENS["high"] == 2048
    assert route_one(_req(effort="low"), REG, IDLE, NOBUDGET).est_max_tokens == 512


def test_decision_is_deterministic():
    req = _req(index=3, effort="xhigh")
    a = route_one(req, REG, BUSY, NOBUDGET)
    b = route_one(req, REG, BUSY, NOBUDGET)
    assert a.model_dump() == b.model_dump()
    assert a.index == 3
