"""MPR role-registry schema (skills/mpr/registry/schema.py) — the pydantic SSOT (Spec 05 §3 / §10).

Pure schema/validation tests (no LLM, no discovery, no network): the panel/role models are closed and
XGrammar-clean, cardinality + uniqueness live in validators (not schema keywords), the slug/empty/typo
guards bite, and ``use_enum_values`` stores plain strings (the form resolve_effort/resolve_policy in
Reg-2 will compare against). The XGrammar lint rides on the real ``ack.case_spec`` (core/ on path).
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from mpr.registry.schema import (
    MAX_ROLES,
    MIN_ROLES,
    EffortDefaults,
    Mode,
    Panel,
    Role,
    panel_json_schema,
    validate_panel_json,
)


def _panel_dict(n_roles: int = 3, **over) -> dict:
    d = {
        "domain": "architecture-decision",
        "mode": "decision",
        "roles": [{"role": f"Role {i}", "lens_prompt": f"lens {i}"} for i in range(n_roles)],
        "effort_defaults": {"default": "high"},
        "evidence_source": "internal",
        "synthesis_template": "decision-matrix",
    }
    d.update(over)
    return d


# ── XGrammar / closed-schema invariants (§1, §3) ─────────────────────────────────────────────────
def test_panel_schema_is_xgrammar_clean():
    from ack.case_spec import lint_schema_for_xgrammar  # core/ on sys.path via conftest

    assert lint_schema_for_xgrammar(panel_json_schema()) == []  # no array-cardinality keywords


def test_panel_schema_is_closed():
    sch = panel_json_schema()
    assert sch.get("additionalProperties") is False
    defs = sch["$defs"]
    assert defs["Role"]["additionalProperties"] is False
    assert defs["EffortDefaults"]["additionalProperties"] is False


def test_panel_json_schema_uses_defs_for_roles():
    sch = panel_json_schema()
    assert "Role" in sch["$defs"] and "EffortDefaults" in sch["$defs"]
    # cardinality is NOT a schema keyword (it lives in the validator)
    assert "minItems" not in sch["properties"]["roles"]
    assert "maxItems" not in sch["properties"]["roles"]


# ── Happy path ───────────────────────────────────────────────────────────────────────────────────
def test_valid_panel_validates():
    p = validate_panel_json(_panel_dict())
    assert isinstance(p, Panel)
    assert len(p.roles) == 3
    assert p.version == 1


def test_use_enum_values_stores_plain_strings():
    p = validate_panel_json(_panel_dict(roles=[
        {"role": "A", "lens_prompt": "x", "effort": "high", "provider_policy": "local-only"},
        {"role": "B", "lens_prompt": "y"},
        {"role": "C", "lens_prompt": "z"},
    ]))
    assert p.mode == "decision" and isinstance(p.mode, str)
    assert p.evidence_source == "internal"
    assert p.roles[0].effort == "high" and isinstance(p.roles[0].effort, str)
    assert p.roles[0].provider_policy == "local-only"


def test_role_optional_fields_default_none():
    r = Role(role="SRE / Ops", lens_prompt="operate it at 3am")
    assert r.effort is None and r.provider_policy is None


# ── Validators bite (§3) ─────────────────────────────────────────────────────────────────────────
def test_panel_rejects_extra_key():
    with pytest.raises(ValidationError):
        validate_panel_json(_panel_dict(bogus=1))


def test_panel_too_few_roles_rejected():
    assert MIN_ROLES == 3
    with pytest.raises(ValidationError, match=r"needs 3\.\.9 roles"):
        validate_panel_json(_panel_dict(n_roles=2))


def test_panel_too_many_roles_rejected():
    assert MAX_ROLES == 9
    with pytest.raises(ValidationError, match=r"needs 3\.\.9 roles"):
        validate_panel_json(_panel_dict(n_roles=10))


def test_duplicate_role_labels_rejected():
    bad = _panel_dict(roles=[
        {"role": "SRE / Ops", "lens_prompt": "a"},
        {"role": "sre / ops", "lens_prompt": "b"},  # same label, case/space-insensitive
        {"role": "Security", "lens_prompt": "c"},
    ])
    with pytest.raises(ValidationError, match="unique"):
        validate_panel_json(bad)


def test_domain_must_be_slug():
    with pytest.raises(ValidationError, match="slug"):
        validate_panel_json(_panel_dict(domain="Foo Bar"))


def test_empty_lens_prompt_rejected():
    bad = _panel_dict(roles=[
        {"role": "A", "lens_prompt": "   "},  # whitespace-only
        {"role": "B", "lens_prompt": "y"},
        {"role": "C", "lens_prompt": "z"},
    ])
    with pytest.raises(ValidationError):
        validate_panel_json(bad)


def test_version_must_be_ge_one():
    with pytest.raises(ValidationError):
        validate_panel_json(_panel_dict(version=0))


# ── EffortDefaults typo guard (§3 / §4 drift gap) ────────────────────────────────────────────────
def test_by_mode_rejects_unknown_mode():
    EffortDefaults(default="high", by_mode={"decision": "high"})  # valid Mode key
    with pytest.raises(ValidationError, match="by_mode keys"):
        EffortDefaults(default="high", by_mode={"decison": "high"})  # typo → rejected


def test_by_mode_accepts_all_mode_values():
    ed = EffortDefaults(default="medium", by_mode={m.value: "high" for m in Mode})
    assert set(ed.by_mode) == {m.value for m in Mode}
