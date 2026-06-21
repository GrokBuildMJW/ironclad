"""decision-matrix template (Spec 06 §4.1) — schema + deterministic render + validate, LLM-free.

The LLM emits a ``DecisionMatrix`` JSON block; MPR computes the weighted score itself (Σ weight×score,
the LLM's own sum is ignored → no arithmetic hallucination), cross-checks the recommendation against
the top score, enforces a mandatory fallback ≠ recommendation, and embeds the conflict zones. Form is
machine-guaranteed; only the rationales/notes are LLM prose.
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

from pydantic import BaseModel, Field

from ..conflicts import Conflict
from .. import i18n
from ._common import conflict_zones_md, extract_json, raw_with_conflicts, warnings_block


class Criterion(BaseModel):
    name: str
    weight: int = Field(ge=1, le=5)
    rationale: Optional[str] = None


class Cell(BaseModel):
    option: str
    criterion: str
    score: int = Field(ge=1, le=5)
    note: Optional[str] = None


class DecisionMatrix(BaseModel):
    options: List[str]
    criteria: List[Criterion]
    cells: List[Cell]
    recommendation: str
    recommendation_rationale: str
    fallback: str
    fallback_trigger: str
    conflict_notes: List[str] = Field(default_factory=list)


def weighted_scores(dm: DecisionMatrix) -> dict:
    """Σ (weight_crit × score_cell) per option — computed by MPR, never trusted from the LLM."""
    cw = {c.name: c.weight for c in dm.criteria}
    scores = {o: 0 for o in dm.options}
    for cell in dm.cells:
        if cell.option in scores and cell.criterion in cw:
            scores[cell.option] += cw[cell.criterion] * cell.score
    return scores


def render_decision(dm: DecisionMatrix, conflicts: List[Conflict], warnings: List[str]) -> str:
    scores = weighted_scores(dm)
    cellmap = {(c.option, c.criterion): c.score for c in dm.cells}
    lines: List[str] = []
    if warnings:
        lines += [warnings_block(warnings), ""]
    lines.append(i18n.t("| Criterion (wt.) | ", "templates", "criterion_header")
                 + " | ".join(dm.options) + " |")
    lines.append("|---|" + "|".join("--:" for _ in dm.options) + "|")
    for crit in dm.criteria:
        row = " | ".join(str(cellmap.get((o, crit.name), "–")) for o in dm.options)
        lines.append(f"| {crit.name} (×{crit.weight}) | {row} |")
    lines.append(i18n.t("| **Weighted score** | ", "templates", "weighted_score")
                 + " | ".join(f"**{scores[o]}**" for o in dm.options) + " |")
    lines.append("")
    lines.append(f"{i18n.t('**Recommendation:**', 'templates', 'recommendation')} "
                 f"**{dm.recommendation}** — {dm.recommendation_rationale}")
    if scores:
        top = max(dm.options, key=lambda o: scores[o])
        if top != dm.recommendation:
            lines.append(i18n.t("> ⚠ Recommendation deviates from the top score ({top}) — see rationale.",
                                "templates", "reco_deviates").format(top=top))
    # The label already says "trigger when"/"auslösen wenn"; the model often restates the conjunction
    # ("Wenn …"/"When …") and ends its trigger with a period → "auslösen wenn Wenn … sind.." (#49).
    # Strip a leading conjunction and emit exactly one terminal period.
    trigger = re.sub(r"^(wenn|falls|when|if)\s+", "", (dm.fallback_trigger or "").strip(), flags=re.IGNORECASE)
    fb_line = (f"{i18n.t('**Fallback:**', 'templates', 'fallback')} {dm.fallback} — "
               f"{i18n.t('trigger when', 'templates', 'fallback_trigger')} {trigger}").rstrip()
    if not fb_line.endswith((".", "!", "?")):
        fb_line += "."
    lines.append(fb_line)
    cz = conflict_zones_md(conflicts)
    if cz:
        lines += ["", cz]
    return "\n".join(lines)


def validate_decision(body: str, conflicts: List[Conflict]) -> Tuple[str, bool]:
    data = extract_json(body)
    if data is None:
        return raw_with_conflicts(body, conflicts), False
    try:
        dm = DecisionMatrix.model_validate(data)
    except Exception:  # noqa: BLE001 — schema-invalid → caller may repair-reask (§4.4)
        return raw_with_conflicts(body, conflicts), False

    warnings: List[str] = []
    if len(dm.options) < 2:
        warnings.append(i18n.t("Fewer than 2 options ({n})", "templates", "warn_few_options").format(n=len(dm.options)))
    if len(dm.criteria) < 2:
        warnings.append(i18n.t("Fewer than 2 criteria ({n})", "templates", "warn_few_criteria").format(n=len(dm.criteria)))
    expected = len(dm.options) * len(dm.criteria)
    # Count UNIQUE cells that actually map onto a declared (option, criterion) pair — not just
    # len(cells). The LLM sometimes emits cells whose option/criterion strings don't match the lists
    # (or none at all): the matrix then renders all "–" with score 0, yet is "complete" by count. That
    # is a degenerate matrix — perspectives gave no scores (e.g. the premise was rejected) — so flag it
    # honestly, otherwise the report carries an invented recommendation as if it were earned (LOK-13).
    opt_set, crit_set = set(dm.options), {c.name for c in dm.criteria}
    mapped = len({(c.option, c.criterion) for c in dm.cells
                  if c.option in opt_set and c.criterion in crit_set})
    if expected and mapped == 0:
        warnings.append(i18n.t("Matrix has no scorable cells — perspectives gave no criterion scores "
                               "(premise possibly rejected); treat the recommendation with caution",
                               "templates", "warn_no_cells"))
    elif mapped < expected:
        warnings.append(i18n.t("Matrix incomplete ({mapped}/{expected} scorable cells)",
                               "templates", "warn_matrix_incomplete").format(mapped=mapped, expected=expected))
    if dm.fallback.strip().lower() == dm.recommendation.strip().lower():
        warnings.append(i18n.t("Fallback = recommendation (no robust fallback identified)",
                               "templates", "warn_fallback_eq_reco"))
    # NOTE (LB-7): we deliberately do NOT warn when a blocking conflict isn't mirrored in conflict_notes.
    # That was internal bookkeeping that leaked into the reader-facing report — and it is redundant: the
    # conflict is rendered in its own "Conflict zones" section AND recorded in the manifest's conflicts list.
    # Reader-relevant caveats (fallback==recommendation, incomplete matrix) DO stay as warnings.

    # The JSON parsed + the schema validated → the rendered matrix is USABLE. Soft quality warnings are
    # rendered inline but must NOT flip valid → that would make synthesis.py discard a good matrix for raw
    # consolidation (Live-Bug #6). Only a hard parse/schema failure (handled above) degrades.
    return render_decision(dm, conflicts, warnings), True
