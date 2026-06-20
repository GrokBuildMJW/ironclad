"""evidence-report template (Spec 06 §4.2) — schema + render + validate, LLM-free.

Confidence tiers are computed DETERMINISTICALLY from support/dissent/conflicts and only ever
*demote* the LLM's tier (never upgrade → conservative): a claim is high only with >=2 supporting
perspectives, >=1 quote, and untouched by a material/blocking conflict; a blocking-touched claim is
forced to low.
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

from pydantic import BaseModel, Field
from typing import Literal

from ..conflicts import Conflict
from ._common import conflict_zones_md, extract_json, raw_with_conflicts, warnings_block

_TIER_ORDER = {"low": 0, "medium": 1, "high": 2}
_SEV_RANK = {"blocking": 0, "material": 1, "minor": 2}
_WORD_RE = re.compile(r"[a-zA-Zäöüß0-9]+")
_STOP = frozenset({"der", "die", "das", "und", "oder", "ist", "sind", "ein", "eine", "the", "a",
                   "an", "and", "or", "is", "are", "be", "to", "of", "for", "with", "in", "on"})


class Citation(BaseModel):
    role: str
    provider: str
    quote: str
    source_ref: Optional[str] = None


class Finding(BaseModel):
    claim: str
    confidence: Literal["high", "medium", "low"]
    support: List[Citation] = Field(default_factory=list)
    dissent: List[str] = Field(default_factory=list)


class EvidenceReport(BaseModel):
    summary: str
    findings: List[Finding]
    conflict_zones: List[str] = Field(default_factory=list)
    open_questions: List[str] = Field(default_factory=list)


def _tokens(text: str) -> set:
    return {w for w in (t.lower() for t in _WORD_RE.findall(text or ""))
            if len(w) >= 3 and w not in _STOP}


def _touched_severity(claim: str, conflicts: List[Conflict]) -> Optional[str]:
    ct = _tokens(claim)
    worst: Optional[str] = None
    for c in conflicts:
        blob = c.topic + " " + " ".join(s.stance for s in c.sides)
        if ct & _tokens(blob):
            if worst is None or _SEV_RANK[c.severity] < _SEV_RANK[worst]:
                worst = c.severity
    return worst


def resolve_tier(f: Finding, conflicts: List[Conflict]) -> str:
    """Deterministic, conservative tier: the LLM tier capped (never raised) by the computed ceiling."""
    n_support = len(f.support)
    n_quotes = sum(1 for c in f.support if (c.quote or "").strip())
    touched = _touched_severity(f.claim, conflicts)
    if n_quotes >= 1 and n_support >= 2 and touched not in ("material", "blocking"):
        ceiling = "high"
    elif (n_quotes >= 1 and n_support >= 1) or n_support >= 2 or touched == "material":
        ceiling = "medium"
    else:
        ceiling = "low"
    if touched == "blocking":
        ceiling = "low"
    # never upgrade: keep the lower of LLM tier and computed ceiling.
    return ceiling if _TIER_ORDER[ceiling] < _TIER_ORDER[f.confidence] else f.confidence


def render_evidence(rep: EvidenceReport, conflicts: List[Conflict], warnings: List[str]) -> str:
    tiers = {f.claim: resolve_tier(f, conflicts) for f in rep.findings}
    lines: List[str] = []
    if warnings:
        lines += [warnings_block(warnings), ""]
    lines += [rep.summary.strip(), ""]
    for tier, head in (("high", "### Hohe Konfidenz"), ("medium", "### Mittlere Konfidenz"),
                       ("low", "### Niedrige Konfidenz / unbelegt")):
        group = [f for f in rep.findings if tiers[f.claim] == tier]
        if not group:
            continue
        lines.append(head)
        for f in group:
            cites = "; ".join(f'{c.role}@{c.provider}: "{c.quote}"' for c in f.support)
            suffix = f"  [{cites}]" if cites else ""
            if f.dissent:
                suffix += f"  (Dissens: {', '.join(f.dissent)})"
            lines.append(f"- {f.claim}{suffix}")
    cz = conflict_zones_md(conflicts)
    if cz:
        lines += ["", cz]
    if rep.open_questions:
        lines += ["", "### Offene Fragen"] + [f"- {q}" for q in rep.open_questions]
    return "\n".join(lines)


def validate_evidence(body: str, conflicts: List[Conflict]) -> Tuple[str, bool]:
    data = extract_json(body)
    if data is None:
        return raw_with_conflicts(body, conflicts), False
    try:
        rep = EvidenceReport.model_validate(data)
    except Exception:  # noqa: BLE001
        return raw_with_conflicts(body, conflicts), False
    warnings: List[str] = []
    if not rep.findings:
        warnings.append("Keine Findings im Report")
    return render_evidence(rep, conflicts, warnings), True  # soft warnings rendered inline, not degrade (LB-6)
