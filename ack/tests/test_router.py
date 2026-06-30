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

# #442: Codex is an EXTERNAL cloud CLI — capabilities.local MUST be false (defaults false), like the
# claude CLI siblings; a SENSITIVE request must never route to it (sovereignty).
CODEX = {"provider_id": "codex", "kind": "cli", "model": "gpt-5.5", "bin": "codex",
         "cmd_template": "{bin} exec -m {model} {prompt}", "capabilities": {"max_effort": "xhigh"}}

REG = load_registry({"providers": {"pool": [SPARK, SONNET, OPUS, KIMI], "default_id": "spark-vllm"}})
REG_NOLOCAL = load_registry({"providers": {"pool": [SONNET, OPUS, KIMI]}})
REG_CODEX = load_registry({"providers": {"pool": [SPARK, CODEX], "default_id": "spark-vllm"}})
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


def test_codex_external_never_chosen_for_sensitive():
    # #442 regression: Codex is an EXTERNAL cloud CLI (capabilities.local false). A SENSITIVE request —
    # even needs_web, even under load — must NEVER route to codex; the pick is either a local provider
    # or None (sovereignty wins over capability), never the external CLI.
    assert REG_CODEX.by_id()["codex"].capabilities.local is False
    for load in (IDLE, BUSY, FULL):
        d = route_one(_req(sensitivity=Sensitivity.SENSITIVE, needs_web=True), REG_CODEX, load, NOBUDGET)
        assert d.provider_id != "codex"                                   # external never serves SENSITIVE
        if d.provider_id is not None:
            assert REG_CODEX.by_id()[d.provider_id].capabilities.local is True


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


def test_max_effort_outside_enum_normalizes_not_raises():
    # ROUTER-1 (#503): a conf max_effort outside the enum must normalize at load to the conservative FLOOR
    # "low" — so the router never KeyErrors on EFFORT_RANK[max_effort] (never-raises-into-the-tool-loop)
    # AND a typo can never OVER-claim capability.
    bad = {"provider_id": "typo-cli", "kind": "cli", "model": "x", "bin": "x",
           "cmd_template": "{bin} {prompt}", "capabilities": {"max_effort": "ultra"}}
    reg = load_registry({"providers": {"pool": [SPARK, bad], "default_id": "spark-vllm"}})
    assert reg.by_id()["typo-cli"].capabilities.max_effort == "low"      # normalized to the floor at load
    d = route_one(_req(effort="high"), reg, IDLE, NOBUDGET)              # must not raise
    assert d.provider_id is not None


def test_decision_is_deterministic():
    req = _req(index=3, effort="xhigh")
    a = route_one(req, REG, BUSY, NOBUDGET)
    b = route_one(req, REG, BUSY, NOBUDGET)
    assert a.model_dump() == b.model_dump()
    assert a.index == 3


# ── #457: SOFT distinct-reviewer anti-affinity (excluded_provider_ids, FORK-F) ────────────────────
def test_distinct_reviewer_none_when_no_exclusion():
    # no exclusion requested → provenance is None and routing is byte-identical to before #457.
    d = route_one(_req(effort="high"), REG_NOLOCAL, IDLE, NOBUDGET)
    assert d.distinct_reviewer is None
    assert d.provider_id == route_one(_req(effort="high"), REG_NOLOCAL, IDLE, NOBUDGET).provider_id


def test_distinct_reviewer_excludes_producer_when_a_peer_remains():
    # a review whose subject was PRODUCED by the would-be winner must route to a DIFFERENT equal peer.
    base = route_one(_req(effort="high"), REG_NOLOCAL, IDLE, NOBUDGET)
    excl = route_one(_req(effort="high", excluded_provider_ids=[base.provider_id]),
                     REG_NOLOCAL, IDLE, NOBUDGET)
    assert excl.provider_id is not None and excl.provider_id != base.provider_id   # not the producer
    assert excl.distinct_reviewer == "applied"


def test_distinct_reviewer_waives_when_producer_is_the_only_capable_peer():
    # SOFT: when excluding the producer would leave NO capable agent, the route is NOT declined — the
    # producer is kept and the waive is recorded (the rule is conditional on a 2nd agent being available).
    everyone = ["spark-vllm", "claude-sonnet", "claude-opus", "kimi-cli"]
    d = route_one(_req(effort="high", excluded_provider_ids=everyone), REG_NOLOCAL, IDLE, NOBUDGET)
    assert d.provider_id in everyone                 # still routed (never None over a SOFT preference)
    assert d.distinct_reviewer == "waived"


def test_distinct_reviewer_never_overrides_sovereignty():
    # HARD sovereignty outranks the SOFT anti-affinity: a SENSITIVE request whose only local provider IS
    # the excluded producer stays LOCAL (waived) — it must never offload to an external "distinct" peer.
    d = route_one(_req(effort="high", sensitivity=Sensitivity.SENSITIVE,
                       excluded_provider_ids=["spark-vllm"]), REG, IDLE, NOBUDGET)
    assert d.provider_id == "spark-vllm"             # never leaks to an external to satisfy anti-affinity
    assert d.reason == "local-sovereign"
    assert d.distinct_reviewer == "waived"


def test_distinct_reviewer_noop_exclusion_is_not_reported_applied():
    # review A (#457): an excluded id that is NOT among the candidates (unknown / case-mismatch) must NOT
    # be reported as "applied" — nothing was dropped, so the provenance stays None (no anti-affinity event).
    d = route_one(_req(effort="high", excluded_provider_ids=["does-not-exist", "CLAUDE-SONNET"]),
                  REG_NOLOCAL, IDLE, NOBUDGET)
    assert d.provider_id is not None
    assert d.distinct_reviewer is None


def test_distinct_reviewer_is_snapshot_stable():
    req = _req(index=5, effort="high", excluded_provider_ids=["codex"])
    a = route_one(req, REG_NOLOCAL, IDLE, NOBUDGET)
    b = route_one(req, REG_NOLOCAL, IDLE, NOBUDGET)
    assert a.model_dump() == b.model_dump()


def test_route_one_honors_configured_effort_max_tokens():
    # providers.effort_max_tokens (threaded from the dispatcher) overrides the module per-effort cap.
    # Regression for the dead-knob bug: the configured table was built into the dispatcher but never read.
    d = route_one(_req(effort="high"), REG, IDLE, NOBUDGET, effort_max_tokens={"high": 9999})
    assert d.est_max_tokens == 9999


def test_route_one_effort_max_tokens_falls_back_per_key_and_on_garbage():
    # a missing key falls back to the module default; a malformed table is ignored — never raises
    assert route_one(_req(effort="high"), REG, IDLE, NOBUDGET,
                     effort_max_tokens={"low": 7}).est_max_tokens == EFFORT_MAX_TOKENS["high"]
    assert route_one(_req(effort="high"), REG, IDLE, NOBUDGET,
                     effort_max_tokens="not-a-dict").est_max_tokens == EFFORT_MAX_TOKENS["high"]
    assert route_one(_req(effort="high"), REG, IDLE, NOBUDGET,
                     effort_max_tokens={"high": True}).est_max_tokens == EFFORT_MAX_TOKENS["high"]
