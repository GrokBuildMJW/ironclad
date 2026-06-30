"""Synthesis-template binding (Spec 05 §5) — the declarative contract template ↔ output-form ↔ mode.

Each panel declares exactly one ``synthesis_template``; the synthesis stage (Spec 06 / unit 1c) reads
it and picks the output format. This module is *only* the binding data + the adhoc default helper — it
renders nothing (that is 1c).

``Mode`` and ``synthesis_template`` are deliberately NOT 1:1 (e.g. ``risk-assessment`` runs in
``evidence-research`` mode but uses ``risk-register``), which is why they are separate fields on the
panel; ``SYNTHESIS_BINDING`` records each template's *typical* mode for documentation/contract, not as
a validation constraint.

Reserved (#503 MPR-REG-2): this is the declarative §5 binding contract (the SSOT for output-form ↔ mode),
covered by the registry contract tests. The runtime synthesis stage selects format from the
``synthesis_template`` enum directly, so it does not read this table at request time — it is kept as the
canonical, machine-checkable contract surface, not as dead code.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Union

from .schema import Mode, SynthesisTemplate


@dataclass(frozen=True)
class SynthesisBinding:
    """One template's contract: what shape it emits + the mode it typically serves."""

    output_form: str       # the shape the synthesis stage renders (Spec 06 consumes this)
    typical_mode: Mode     # the mode this template usually serves (NOT a 1:1 constraint)


# ── The §5 binding table (Single-Source for the synthesis-template contract) ──────────────────────
SYNTHESIS_BINDING: dict[SynthesisTemplate, SynthesisBinding] = {
    SynthesisTemplate.DECISION_MATRIX: SynthesisBinding(
        output_form="weighted criteria matrix + recommendation + reversibility/exit",
        typical_mode=Mode.DECISION,
    ),
    SynthesisTemplate.EVIDENCE_REPORT: SynthesisBinding(
        output_form="confidence tiers + conflict zones + citations/provenance",
        typical_mode=Mode.EVIDENCE_RESEARCH,
    ),
    SynthesisTemplate.COMPARISON_MATRIX: SynthesisBinding(
        output_form="competitors x dimensions + gaps/opportunities",
        typical_mode=Mode.COMPARISON,
    ),
    SynthesisTemplate.RISK_REGISTER: SynthesisBinding(
        output_form="risks x {severity, likelihood, mitigation, owner}",
        typical_mode=Mode.EVIDENCE_RESEARCH,  # not 1:1 with evidence-report
    ),
}


def binding_for(template: Union[SynthesisTemplate, str]) -> SynthesisBinding:
    """Look up the binding for a template (accepts the enum or its string value)."""
    return SYNTHESIS_BINDING[SynthesisTemplate(template)]


def output_form(template: Union[SynthesisTemplate, str]) -> str:
    return binding_for(template).output_form


def typical_mode(template: Union[SynthesisTemplate, str]) -> Mode:
    return binding_for(template).typical_mode


def default_template_for_mode(mode: Union[Mode, str]) -> SynthesisTemplate:
    """The adhoc default (Spec 04 §8): a generated panel has no declared template, so it follows the
    mode — ``decision`` → ``decision-matrix``, everything else → ``evidence-report`` (the conservative
    generic form). Declared panels (e.g. ``competitive`` → ``comparison-matrix``) set their template
    explicitly and never go through this helper.
    """
    return (
        SynthesisTemplate.DECISION_MATRIX
        if Mode(mode) == Mode.DECISION
        else SynthesisTemplate.EVIDENCE_REPORT
    )
