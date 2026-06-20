"""Sovereignty + security engine (skills/mpr/sovereignty.py) — Spec 09 §4/§5/§6 / §9.2,§9.3.

The central guarantee: local-only never lands external (resolved fail-closed, hard-guarded pre-dispatch,
monotonic upgrade-only); permission is downgrade-only; effort is clamped to the provider's levels.
"""
from __future__ import annotations

import pytest

from mpr.sovereignty import (
    ProviderChoice,
    SovereigntyViolation,
    assert_sovereign,
    choose_provider,
    clamp_effort,
    effective_permission,
    min_restrictive,
    plan_perspective_dispatch,
    resolve_sovereignty,
)

POOL = {
    "spark-vllm": {"policy_class": "local-only", "effort_levels": ["low", "medium", "high"]},
    "claude-sonnet": {"policy_class": "offloadable", "effort_levels": ["low", "medium", "high"]},
    "claude-opus": {"policy_class": "offloadable", "effort_levels": ["high", "xhigh"]},
}
ROUTING = {"spill_when_spark_busy": True,
           "effort_to_provider": {"low": "spark-vllm", "medium": "claude-sonnet",
                                  "high": "claude-sonnet", "xhigh": "claude-opus"}}
OFFLOAD = "claude-sonnet"


def _plan(role_policy, effort="medium", evidence="external", repo=False, busy=False, perm="acceptEdits",
          **sov) -> ProviderChoice:
    return plan_perspective_dispatch(
        role="R", role_policy=role_policy, effort=effort, evidence_source=evidence,
        reads_repo_context=repo, pool=POOL, routing=ROUTING, default_offload=OFFLOAD,
        operator_permission=perm, spark_busy=busy, **sov)


# ── §5.1 resolution (upgrade-only, fail-closed) ───────────────────────────────────────────────────
def test_internal_evidence_forces_local_only():
    assert resolve_sovereignty(role_policy="offloadable", evidence_source="internal") == "local-only"
    assert resolve_sovereignty(role_policy="offloadable", evidence_source="mixed") == "local-only"


def test_repo_context_forces_local_only():
    assert resolve_sovereignty(role_policy="offloadable", evidence_source="external",
                               reads_repo_context=True) == "local-only"


def test_sovereignty_never_downgrades():
    # a local-only role stays local-only across every config combination.
    for ev in ("internal", "external", "mixed"):
        for iilo in (True, False):
            for fc in (True, False):
                assert resolve_sovereignty(role_policy="local-only", evidence_source=ev,
                                           internal_is_local_only=iilo, fail_closed=fc) == "local-only"


def test_fail_closed_on_ambiguous_policy():
    assert resolve_sovereignty(role_policy="weird", evidence_source="external", fail_closed=True) == "local-only"
    assert resolve_sovereignty(role_policy="weird", evidence_source="external", fail_closed=False) == "offloadable"


def test_default_policy_when_role_none():
    assert resolve_sovereignty(role_policy=None, evidence_source="external", default_policy="offloadable") == "offloadable"


# ── §5.2 hard guard ───────────────────────────────────────────────────────────────────────────────
def test_local_only_never_dispatched_external():
    c = _plan("local-only", evidence="external")
    assert c.provider == "spark-vllm" and c.policy == "local-only"  # never an external provider


def test_plan_never_returns_external_for_local_only_any_effort():
    for eff in ("low", "medium", "high", "xhigh"):
        for busy in (True, False):
            c = _plan("local-only", effort=eff, busy=busy)
            assert POOL[c.provider]["policy_class"] == "local-only"


def test_sovereignty_violation_is_fail_closed():
    # the defense-in-depth guard raises BEFORE any dispatch if a local-only lands on an external provider.
    with pytest.raises(SovereigntyViolation, match="local-only but provider"):
        assert_sovereign("R", "local-only", "claude-sonnet", POOL)


# ── §6.1 provider choice ──────────────────────────────────────────────────────────────────────────
def test_choose_low_offload_stays_local_when_idle():
    assert choose_provider(policy="offloadable", effort="low", pool=POOL, routing=ROUTING,
                           default_offload=OFFLOAD, spark_busy=False) == "spark-vllm"


def test_choose_spill_when_busy():
    assert choose_provider(policy="offloadable", effort="medium", pool=POOL, routing=ROUTING,
                           default_offload=OFFLOAD, spark_busy=True) == "claude-sonnet"


def test_choose_offload_high_to_sonnet_xhigh_to_opus():
    assert choose_provider(policy="offloadable", effort="high", pool=POOL, routing=ROUTING,
                           default_offload=OFFLOAD) == "claude-sonnet"
    assert choose_provider(policy="offloadable", effort="xhigh", pool=POOL, routing=ROUTING,
                           default_offload=OFFLOAD) == "claude-opus"


# ── §4.2 permission downgrade-only (M6) ──────────────────────────────────────────────────────────
def test_permission_downgrade_only():
    assert effective_permission("acceptEdits") == "plan"
    assert effective_permission("bypassPermissions") == "plan"
    assert effective_permission("default") == "plan"
    assert effective_permission("plan") == "plan"
    assert effective_permission("") == "plan"            # empty → fail-closed
    assert effective_permission("nonsense") == "plan"    # unknown → fail-closed
    assert min_restrictive("plan", "acceptEdits") == "plan"
    assert min_restrictive("foo", "bar") == "plan"   # MED-1: two unknowns → valid mode, not raw string


# ── §6.2 effort clamp (M3) ────────────────────────────────────────────────────────────────────────
def test_effort_clamped_to_provider_levels():
    assert clamp_effort("xhigh", ["low", "medium", "high"]) == "high"   # unsupported → highest accepted
    assert clamp_effort("high", ["high", "xhigh"]) == "high"            # supported → unchanged
    assert clamp_effort("medium", []) == "medium"                       # no levels → as-is


def test_plan_offload_renders_plan_and_clamps_effort():
    # xhigh offload → opus (accepts xhigh) → effort stays xhigh, permission downgraded to plan.
    c = _plan("offloadable", effort="xhigh", evidence="external", busy=True)
    assert c.provider == "claude-opus" and c.effort == "xhigh" and c.permission == "plan"


def test_plan_offload_effort_clamped_when_provider_lacks_level():
    # force routing to sonnet (no xhigh) at xhigh → effort clamped to high.
    routing = {"spill_when_spark_busy": True,
               "effort_to_provider": {"xhigh": "claude-sonnet"}}
    c = plan_perspective_dispatch(role="R", role_policy="offloadable", effort="xhigh",
                                  evidence_source="external", reads_repo_context=False, pool=POOL,
                                  routing=routing, default_offload=OFFLOAD,
                                  operator_permission="acceptEdits", spark_busy=True)
    assert c.provider == "claude-sonnet" and c.effort == "high"   # xhigh not in sonnet levels → high
