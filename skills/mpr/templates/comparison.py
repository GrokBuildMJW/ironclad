"""comparison-matrix template (Spec 06 §4.3) — like decision-matrix, no forced recommendation.

Structure = the decision matrix without ``fallback``/``recommendation``; instead the mandatory
"Gaps/Opportunities" sections (02 §7 competitive). Weighted score computed by MPR;
conflict zones embedded identically.
"""
from __future__ import annotations

from typing import List, Tuple

from pydantic import BaseModel, Field

from ..conflicts import Conflict
from .. import i18n
from ._common import conflict_zones_md, extract_json, raw_with_conflicts, warnings_block
from .decision import Cell, Criterion


class ComparisonMatrix(BaseModel):
    options: List[str]
    criteria: List[Criterion]
    cells: List[Cell]
    gaps: List[str] = Field(default_factory=list)
    opportunities: List[str] = Field(default_factory=list)
    conflict_notes: List[str] = Field(default_factory=list)


def _scores(cm: ComparisonMatrix) -> dict:
    cw = {c.name: c.weight for c in cm.criteria}
    scores = {o: 0 for o in cm.options}
    for cell in cm.cells:
        if cell.option in scores and cell.criterion in cw:
            scores[cell.option] += cw[cell.criterion] * cell.score
    return scores


def render_comparison(cm: ComparisonMatrix, conflicts: List[Conflict], warnings: List[str]) -> str:
    scores = _scores(cm)
    cellmap = {(c.option, c.criterion): c.score for c in cm.cells}
    lines: List[str] = []
    if warnings:
        lines += [warnings_block(warnings), ""]
    lines.append(i18n.t("| Dimension (wt.) | ", "templates", "dimension_header")
                 + " | ".join(cm.options) + " |")
    lines.append("|---|" + "|".join("--:" for _ in cm.options) + "|")
    for crit in cm.criteria:
        row = " | ".join(str(cellmap.get((o, crit.name), "–")) for o in cm.options)
        lines.append(f"| {crit.name} (×{crit.weight}) | {row} |")
    lines.append(i18n.t("| **Weighted score** | ", "templates", "weighted_score")
                 + " | ".join(f"**{scores[o]}**" for o in cm.options) + " |")
    lines += ["", i18n.t("### Gaps", "templates", "gaps")] + [f"- {g}" for g in cm.gaps]
    lines += ["", i18n.t("### Opportunities", "templates", "opportunities")] + [f"- {o}" for o in cm.opportunities]
    cz = conflict_zones_md(conflicts)
    if cz:
        lines += ["", cz]
    return "\n".join(lines)


def validate_comparison(body: str, conflicts: List[Conflict]) -> Tuple[str, bool]:
    data = extract_json(body)
    if data is None:
        return raw_with_conflicts(body, conflicts), False
    try:
        cm = ComparisonMatrix.model_validate(data)
    except Exception:  # noqa: BLE001
        return raw_with_conflicts(body, conflicts), False
    warnings: List[str] = []
    if len(cm.options) < 2:
        warnings.append(i18n.t("Fewer than 2 options ({n})", "templates", "warn_few_options").format(n=len(cm.options)))
    if len(cm.criteria) < 2:
        warnings.append(i18n.t("Fewer than 2 dimensions ({n})", "templates", "warn_few_dimensions").format(n=len(cm.criteria)))
    expected = len(cm.options) * len(cm.criteria)
    if len(cm.cells) != expected:
        warnings.append(i18n.t("Matrix incomplete ({mapped}/{expected} cells)", "templates",
                               "warn_matrix_incomplete_cmp").format(mapped=len(cm.cells), expected=expected))
    if not cm.gaps:
        warnings.append(i18n.t("Required section 'Gaps' missing", "templates", "warn_missing_gaps"))
    if not cm.opportunities:
        warnings.append(i18n.t("Required section 'Opportunities' missing", "templates", "warn_missing_opportunities"))
    return render_comparison(cm, conflicts, warnings), True  # soft warnings rendered inline, not degrade (LB-6)
