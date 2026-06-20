"""MPR effort & sovereignty resolution (skills/mpr/registry/resolve.py) — Spec 05 §4 / §10.

Deterministic, no LLM: effort precedence (role > by_mode > default), the effort→max_tokens execution
table (the only reasoning-only lever) + the code-CLI {effort} mapping (xhigh→high), policy derivation
from evidence_source, and the registry-local sovereignty defensive — an internal panel never resolves
to offloadable, a contradictory override is hard-rejected.
"""
from __future__ import annotations

import pytest

from mpr.registry.resolve import (
    EFFORT_MAX_TOKENS,
    SovereigntyError,
    effort_to_max_tokens,
    effort_to_template,
    resolve_effort,
    resolve_policy,
)
from mpr.registry.schema import Effort, Panel, ProviderPolicy, Role


def _panel(evidence_source="internal", default="medium", by_mode=None, roles=None, **over) -> Panel:
    data = {
        "domain": "architecture-decision",
        "mode": "decision",
        "roles": roles or [
            {"role": "A", "lens_prompt": "x"},
            {"role": "B", "lens_prompt": "y"},
            {"role": "C", "lens_prompt": "z"},
        ],
        "effort_defaults": {"default": default, "by_mode": by_mode or {}},
        "evidence_source": evidence_source,
        "synthesis_template": "decision-matrix",
    }
    data.update(over)
    return Panel.model_validate(data)


# ── Effort resolution precedence (§4) ────────────────────────────────────────────────────────────
def test_effort_resolution_precedence_role_wins():
    p = _panel(default="low", by_mode={"decision": "medium"})
    role = Role(role="R", lens_prompt="l", effort="high")
    assert resolve_effort(p, role, mode="decision") == Effort.HIGH


def test_effort_resolution_bymode_over_default():
    p = _panel(default="low", by_mode={"decision": "high"})
    role = Role(role="R", lens_prompt="l")  # no own effort
    assert resolve_effort(p, role, mode="decision") == Effort.HIGH


def test_effort_resolution_falls_to_default():
    p = _panel(default="medium", by_mode={"comparison": "high"})
    role = Role(role="R", lens_prompt="l")
    assert resolve_effort(p, role, mode="decision") == Effort.MEDIUM  # decision not in by_mode
    assert resolve_effort(p, role, mode=None) == Effort.MEDIUM        # no mode → default


def test_effort_resolution_accepts_enum_mode():
    from mpr.registry.schema import Mode

    p = _panel(default="low", by_mode={"decision": "xhigh"})
    role = Role(role="R", lens_prompt="l")
    assert resolve_effort(p, role, mode=Mode.DECISION) == Effort.XHIGH


# ── Effort → execution mapping (§4) ──────────────────────────────────────────────────────────────
def test_effort_maps_to_max_tokens():
    assert effort_to_max_tokens("low") == 2048
    assert effort_to_max_tokens("medium") == 4096
    assert effort_to_max_tokens("high") == 8192
    assert effort_to_max_tokens("xhigh") == 16384
    assert effort_to_max_tokens(Effort.HIGH) == 8192  # enum accepted too


def test_xhigh_maps_to_high_template_but_largest_budget():
    # the {effort} template knows no xhigh → high; the larger token budget carries the extra effort.
    assert effort_to_template("xhigh") == "high"
    assert effort_to_template("high") == "high"
    assert effort_to_max_tokens("xhigh") > effort_to_max_tokens("high")


def test_effort_to_max_tokens_accepts_override_table():
    custom = {"low": 1, "medium": 2, "high": 3, "xhigh": 4}
    assert effort_to_max_tokens("high", table=custom) == 3
    assert EFFORT_MAX_TOKENS["high"] == 8192  # module default untouched


# ── Policy resolution from evidence_source (§4) ──────────────────────────────────────────────────
def test_policy_resolution_from_evidence_source():
    role = Role(role="R", lens_prompt="l")  # no override → derive
    assert resolve_policy(_panel(evidence_source="internal"), role) == ProviderPolicy.LOCAL_ONLY
    assert resolve_policy(_panel(evidence_source="external"), role) == ProviderPolicy.OFFLOADABLE
    assert resolve_policy(_panel(evidence_source="mixed"), role) == ProviderPolicy.OFFLOADABLE


def test_role_local_only_override_on_mixed_panel_allowed():
    # the risk-assessment technical-role pattern: more restrictive override is always allowed.
    p = _panel(evidence_source="mixed")
    role = Role(role="Technical", lens_prompt="l", provider_policy="local-only")
    assert resolve_policy(p, role) == ProviderPolicy.LOCAL_ONLY


def test_role_offloadable_override_on_external_panel_allowed():
    p = _panel(evidence_source="external")
    role = Role(role="R", lens_prompt="l", provider_policy="offloadable")
    assert resolve_policy(p, role) == ProviderPolicy.OFFLOADABLE


# ── Sovereignty defensive (§4, security-critical) ────────────────────────────────────────────────
def test_internal_panel_never_resolves_offloadable_via_derivation():
    # no role on an internal panel may derive to offloadable.
    p = _panel(evidence_source="internal")
    for r in p.roles:
        assert resolve_policy(p, r) == ProviderPolicy.LOCAL_ONLY


def test_internal_panel_contradictory_offloadable_override_hard_rejected():
    p = _panel(evidence_source="internal")
    bad = Role(role="Leaky", lens_prompt="l", provider_policy="offloadable")
    with pytest.raises(SovereigntyError, match="internal evidence must never be offloaded"):
        resolve_policy(p, bad)


def test_internal_panel_local_only_override_ok():
    p = _panel(evidence_source="internal")
    role = Role(role="R", lens_prompt="l", provider_policy="local-only")
    assert resolve_policy(p, role) == ProviderPolicy.LOCAL_ONLY


def test_resolve_policy_returns_enum_member():
    p = _panel(evidence_source="external")
    assert isinstance(resolve_policy(p, p.roles[0]), ProviderPolicy)
