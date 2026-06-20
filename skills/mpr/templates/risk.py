"""risk-register template (Spec 06 §4 + Spec 05 §5 binding) — risks × {severity, likelihood, mitigation, owner}.

Used by the risk-assessment start-panel (Spec 05 §7.4), which runs in ``evidence-research`` mode but
emits a risk register rather than an evidence report (mode and template are deliberately not 1:1). LLM-
free, like the sibling templates: MPR parses the JSON block and renders the table deterministically;
severity/likelihood are ordinal and the register is sorted worst-exposure-first so the gravest risk
leads. Conflict zones are embedded verbatim and never trimmed.
"""
from __future__ import annotations

from typing import List, Literal, Optional, Tuple

from pydantic import BaseModel, Field

from ..conflicts import Conflict
from ._common import conflict_zones_md, extract_json, raw_with_conflicts, warnings_block

_LEVEL = {"high": 2, "medium": 1, "low": 0}            # severity/likelihood ordinal (worst-first sort)
_LEVEL_LABEL = {"high": "hoch", "medium": "mittel", "low": "niedrig"}


class Risk(BaseModel):
    risk: str
    severity: Literal["high", "medium", "low"]
    likelihood: Literal["high", "medium", "low"]
    mitigation: str = ""
    owner: Optional[str] = None
    roles: List[str] = Field(default_factory=list)     # which lens(es) raised it


class RiskRegister(BaseModel):
    summary: str
    risks: List[Risk]
    conflict_zones: List[str] = Field(default_factory=list)
    open_questions: List[str] = Field(default_factory=list)


def _exposure(r: Risk) -> int:
    """Worst-first ordering key: severity dominates, likelihood breaks the tie."""
    return _LEVEL[r.severity] * 3 + _LEVEL[r.likelihood]


def render_risk(rr: RiskRegister, conflicts: List[Conflict], warnings: List[str]) -> str:
    lines: List[str] = []
    if warnings:
        lines += [warnings_block(warnings), ""]
    lines += [rr.summary.strip(), ""]
    lines.append("| Risiko | Schwere | Eintritt | Mitigation | Owner |")
    lines.append("|---|:--:|:--:|---|---|")
    for r in sorted(rr.risks, key=_exposure, reverse=True):
        sev, lik = _LEVEL_LABEL[r.severity], _LEVEL_LABEL[r.likelihood]
        roles = f" _({', '.join(r.roles)})_" if r.roles else ""
        lines.append(f"| {r.risk}{roles} | {sev} | {lik} | {r.mitigation or '–'} | {r.owner or '–'} |")
    cz = conflict_zones_md(conflicts)
    if cz:
        lines += ["", cz]
    if rr.open_questions:
        lines += ["", "### Offene Fragen"] + [f"- {q}" for q in rr.open_questions]
    return "\n".join(lines)


def validate_risk(body: str, conflicts: List[Conflict]) -> Tuple[str, bool]:
    data = extract_json(body)
    if data is None:
        return raw_with_conflicts(body, conflicts), False
    try:
        rr = RiskRegister.model_validate(data)
    except Exception:  # noqa: BLE001
        return raw_with_conflicts(body, conflicts), False
    warnings: List[str] = []
    if not rr.risks:
        warnings.append("Kein Risiko im Register")
    n_missing = sum(1 for r in rr.risks if not (r.mitigation or "").strip())
    if n_missing:
        warnings.append(f"{n_missing} Risiko(en) ohne Mitigation")
    return render_risk(rr, conflicts, warnings), True  # soft warnings rendered inline, not degrade (LB-6)
