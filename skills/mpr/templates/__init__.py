"""MPR output templates (Spec 06 §4) — per-mode schema + deterministic render + validate.

``validate_template(template, body, conflicts) -> (rendered, valid)`` dispatches to the right template;
all are LLM-free (the synthesis pipeline owns the repair re-ask on ``valid=False``, §4.4 step 2). MPR
renders the form itself from the parsed schema; the LLM only supplies rationales/summary/notes.
"""
from __future__ import annotations

from typing import List, Tuple

from ..conflicts import Conflict
from ._common import raw_with_conflicts
from .comparison import ComparisonMatrix, render_comparison, validate_comparison
from .decision import (
    Cell,
    Criterion,
    DecisionMatrix,
    render_decision,
    validate_decision,
    weighted_scores,
)
from .evidence import (
    Citation,
    EvidenceReport,
    Finding,
    render_evidence,
    resolve_tier,
    validate_evidence,
)
from .risk import Risk, RiskRegister, render_risk, validate_risk

_VALIDATORS = {
    "decision-matrix": validate_decision,
    "evidence-report": validate_evidence,
    "comparison-matrix": validate_comparison,
    "risk-register": validate_risk,
}


def validate_template(template: str, body: str, conflicts: List[Conflict]) -> Tuple[str, bool]:
    """Parse + render + validate the LLM body for *template*; (rendered_markdown, valid)."""
    fn = _VALIDATORS.get(template)
    if fn is None:
        return raw_with_conflicts(body, conflicts), False  # unknown template → best-effort
    return fn(body, conflicts)


__all__ = [
    "validate_template",
    "DecisionMatrix", "Criterion", "Cell", "validate_decision", "render_decision", "weighted_scores",
    "EvidenceReport", "Finding", "Citation", "validate_evidence", "render_evidence", "resolve_tier",
    "ComparisonMatrix", "validate_comparison", "render_comparison",
    "RiskRegister", "Risk", "validate_risk", "render_risk",
]
