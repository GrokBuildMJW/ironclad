"""MPR synthesis-template binding (skills/mpr/registry/synthesis.py) — Spec 05 §5 / Spec 04 §8.

Deterministic contract tests: every template has a binding, the typical-mode table matches §5, mode
and template are NOT 1:1, the binding accepts the string form a validated Panel stores, and the adhoc
default helper follows the §8 binary rule (decision→decision-matrix, else evidence-report).
"""
from __future__ import annotations

import pytest

from mpr.registry.schema import Mode, SynthesisTemplate
from mpr.registry.synthesis import (
    SYNTHESIS_BINDING,
    binding_for,
    default_template_for_mode,
    output_form,
    typical_mode,
)


def test_binding_covers_all_templates():
    assert set(SYNTHESIS_BINDING) == set(SynthesisTemplate)


def test_binding_typical_modes_match_spec_5():
    assert typical_mode(SynthesisTemplate.DECISION_MATRIX) == Mode.DECISION
    assert typical_mode(SynthesisTemplate.EVIDENCE_REPORT) == Mode.EVIDENCE_RESEARCH
    assert typical_mode(SynthesisTemplate.COMPARISON_MATRIX) == Mode.COMPARISON
    assert typical_mode(SynthesisTemplate.RISK_REGISTER) == Mode.EVIDENCE_RESEARCH


def test_mode_and_template_not_one_to_one():
    # both evidence-report and risk-register serve evidence-research → proves the fields are separate.
    er = typical_mode(SynthesisTemplate.EVIDENCE_REPORT)
    rr = typical_mode(SynthesisTemplate.RISK_REGISTER)
    assert er == rr == Mode.EVIDENCE_RESEARCH


def test_output_form_nonempty_for_all():
    for t in SynthesisTemplate:
        assert output_form(t).strip()


def test_binding_for_accepts_string_value():
    # a validated Panel stores synthesis_template as a plain string (use_enum_values) — must resolve.
    assert binding_for("decision-matrix").typical_mode == Mode.DECISION
    assert output_form("risk-register").startswith("risks")


def test_binding_for_unknown_raises():
    with pytest.raises(ValueError):
        binding_for("no-such-template")


# ── adhoc default (Spec 04 §8, binary) ───────────────────────────────────────────────────────────
def test_default_template_for_mode_decision():
    assert default_template_for_mode(Mode.DECISION) == SynthesisTemplate.DECISION_MATRIX
    assert default_template_for_mode("decision") == SynthesisTemplate.DECISION_MATRIX


def test_default_template_for_mode_else_is_evidence_report():
    assert default_template_for_mode(Mode.EVIDENCE_RESEARCH) == SynthesisTemplate.EVIDENCE_REPORT
    # comparison adhoc → evidence-report (the §8 rule is binary, not the §5 typical-mode map)
    assert default_template_for_mode(Mode.COMPARISON) == SynthesisTemplate.EVIDENCE_REPORT
    assert default_template_for_mode("comparison") == SynthesisTemplate.EVIDENCE_REPORT
