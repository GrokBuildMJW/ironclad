"""Registry config (skills/mpr/registry/config.py) — the mpr.* registry surface (Spec 05 §8 / §10).

Deterministic: defaults mirror the owning modules' constants, each key overrides via the nested mpr
section, a partial effort table merges onto the default, adaptive_min_roles ties to roles_min, and the
two-tier bounds + range validations bite (roles_min>=2, roles_max<=9, min<=max, overlap in [0,1],
positive int effort values).
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from mpr.registry.config import RegistryConfig, load_registry_config
from mpr.registry.guards import DISTINCTNESS_MAX_OVERLAP
from mpr.registry.resolve import EFFORT_MAX_TOKENS
from mpr.registry.schema import MAX_ROLES, MIN_ROLES


# ── defaults ─────────────────────────────────────────────────────────────────────────────────────
def test_defaults_mirror_module_constants():
    cfg = load_registry_config(None)
    assert cfg.panels_dir is None
    assert cfg.roles_min == MIN_ROLES == 3
    assert cfg.roles_max == MAX_ROLES == 9
    assert cfg.effort_max_tokens == EFFORT_MAX_TOKENS
    assert cfg.distinct_max_overlap == DISTINCTNESS_MAX_OVERLAP == 0.7
    assert cfg.adaptive_min_roles == cfg.roles_min  # ties to roles_min


def test_empty_section_is_defaults():
    assert load_registry_config({}) == load_registry_config(None)


# ── per-key override ─────────────────────────────────────────────────────────────────────────────
def test_panels_dir_override():
    cfg = load_registry_config({"panels": {"dir": "/custom/panels"}})
    assert cfg.panels_dir == "/custom/panels"


def test_roles_bounds_override():
    cfg = load_registry_config({"roles": {"min": 4, "max": 6}})
    assert cfg.roles_min == 4 and cfg.roles_max == 6
    assert cfg.adaptive_min_roles == 4  # default ties to the overridden roles_min


def test_distinct_overlap_override():
    cfg = load_registry_config({"distinctness": {"max_overlap": 0.55}})
    assert cfg.distinct_max_overlap == 0.55


def test_adaptive_min_roles_explicit():
    cfg = load_registry_config({"roles": {"min": 3, "max": 8}, "adaptive": {"min_roles": 5}})
    assert cfg.adaptive_min_roles == 5


def test_effort_max_tokens_partial_merge():
    cfg = load_registry_config({"effort": {"max_tokens": {"low": 1000}}})
    assert cfg.effort_max_tokens["low"] == 1000          # overridden
    assert cfg.effort_max_tokens["high"] == EFFORT_MAX_TOKENS["high"]  # others keep defaults
    assert set(cfg.effort_max_tokens) == set(EFFORT_MAX_TOKENS)        # still complete


# ── validation bites ─────────────────────────────────────────────────────────────────────────────
def test_roles_min_below_two_rejected():
    with pytest.raises(ValidationError, match="roles_min must be >= 2"):
        load_registry_config({"roles": {"min": 1}})


def test_roles_max_above_ceiling_rejected():
    with pytest.raises(ValidationError, match="structural ceiling"):
        load_registry_config({"roles": {"max": 10}})


def test_roles_min_greater_than_max_rejected():
    with pytest.raises(ValidationError, match="must be <= roles_max"):
        load_registry_config({"roles": {"min": 6, "max": 4}})


def test_overlap_out_of_range_rejected():
    with pytest.raises(ValidationError, match=r"\[0, 1\]"):
        load_registry_config({"distinctness": {"max_overlap": 1.5}})


def test_effort_non_positive_rejected():
    with pytest.raises(ValidationError, match="positive ints"):
        load_registry_config({"effort": {"max_tokens": {"low": -5}}})


def test_adaptive_min_roles_out_of_range_rejected():
    with pytest.raises(ValidationError, match="adaptive_min_roles"):
        load_registry_config({"roles": {"min": 3, "max": 5}, "adaptive": {"min_roles": 9}})


def test_effort_missing_key_rejected_on_direct_construction():
    with pytest.raises(ValidationError, match="missing keys"):
        RegistryConfig(effort_max_tokens={"low": 1, "medium": 2})  # incomplete table


def test_extra_key_forbidden():
    with pytest.raises(ValidationError):
        RegistryConfig(bogus=1)
