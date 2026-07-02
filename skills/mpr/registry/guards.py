"""Panel guards — Distinctness & Coverage (Spec 05 §7.6).

Deterministic, LLM-free checks that BOTH a declared panel (test-time) and an adaptively generated one
(run-time, Reg-6) must pass. They return *findings* (empty == ok) — the router/adaptive generator turn
a finding into "drop a clone" / "pull a missing role". The shared tokeniser (``lens_signature`` +
``jaccard``) is exported so the router's own distinctness guard (Spec 04 §6.1) reuses one definition.

* **Distinctness** — roles must be genuinely different lenses, not rephrasings: any pair of
  ``lens_prompt``s whose Jaccard token overlap exceeds ``DISTINCTNESS_MAX_OVERLAP`` (default 0.7) is a
  finding. (Identical role *labels* are already hard-rejected in ``Panel._roles_well_formed``.)
* **Coverage** — the panel must cover the domain's expected dimensions (``COVERAGE_AXES``); a missing
  axis is a finding. Axis detection is substring-based on the role+lens text (robust to German
  morphology, e.g. "wart" ⊂ "Wartbarkeit"). For an unknown/adhoc domain there are no reference axes →
  no findings (distinctness + MIN_ROLES carry quality there).
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

from .schema import Panel, Role

# ── Distinctness ─────────────────────────────────────────────────────────────────────────────────
DISTINCTNESS_MAX_OVERLAP = 0.7  # config-overridable via mpr.distinctness.max_overlap (§8)

#: Function words (DE+EN) filtered before the Jaccard so only content words drive the overlap.
_STOPWORDS = frozenset({
    "der", "die", "das", "den", "dem", "des", "ein", "eine", "einen", "einem", "einer", "eines",
    "und", "oder", "aber", "ist", "sind", "war", "wird", "werden", "sein", "nicht", "kein", "keine",
    "für", "mit", "von", "vom", "zu", "zum", "zur", "im", "in", "auf", "an", "als", "bei", "aus",
    "über", "unter", "nach", "vor", "durch", "gegen", "ohne", "um", "wie", "was", "wo", "wer", "wann",
    "welche", "welcher", "welches", "diese", "dieser", "dieses", "diesem", "du", "bist", "sich", "es",
    "man", "pro", "je", "allein", "ausschließlich", "konkrete", "konkret", "nenne", "bewerte",
    "bewertest", "the", "a", "an", "and", "or", "is", "are", "be", "to", "of", "for", "with", "on",
    "in", "at", "as", "by", "this", "that", "you", "your", "it", "its", "which", "what", "how", "not",
})

#: Light suffix trim (DE+EN) so "Wartung"/"Wartbarkeit" cluster — a cheap stem, not a full stemmer.
_SUFFIXES = ("ungen", "ung", "keit", "heit", "barkeit", "lich", "isch", "bar", "en", "er", "es",
             "st", "ing", "tion", "ed", "ly")


def _stem(tok: str) -> str:
    # #503 MPR-REG-4: the LONGEST matching suffix wins (order-independent), so "Wartbarkeit" trims
    # "barkeit" → "wart" and clusters with "Wartung"/"wartbar". A first-match scan stripped the shorter
    # "keit" first ("Wartbarkeit" → "wartbar"), leaving the longer suffixes ("barkeit") unreachable.
    best = ""
    for suf in _SUFFIXES:
        if len(suf) > len(best) and len(tok) > len(suf) + 2 and tok.endswith(suf):
            best = suf
    return tok[: -len(best)] if best else tok


def _tokenize(text: str) -> set[str]:
    toks = re.findall(r"[a-zA-Zäöüß0-9]+", text.lower())
    return {_stem(t) for t in toks if len(t) >= 3 and t not in _STOPWORDS}


def lens_signature(role: Role) -> set[str]:
    """Normalised bag-of-words from a role's label + lens_prompt (the distinctness signature)."""
    return _tokenize(f"{role.role} {role.lens_prompt}")


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def check_distinctness(panel: Panel, *, max_overlap: float = DISTINCTNESS_MAX_OVERLAP) -> list[str]:
    """Findings (empty == ok): role pairs whose lens overlap exceeds *max_overlap* are rephrasings."""
    findings: list[str] = []
    sigs = [(r.role, lens_signature(r)) for r in panel.roles]
    for i in range(len(sigs)):
        for j in range(i + 1, len(sigs)):
            (ri, si), (rj, sj) = sigs[i], sigs[j]
            if jaccard(si, sj) > max_overlap:
                findings.append(f"roles {ri!r} and {rj!r} are rephrasings, not distinct lenses")
    return findings


# ── Coverage ─────────────────────────────────────────────────────────────────────────────────────
#: Reference axes per domain (Spec 05 §7.6) — the list check_coverage holds a panel against.
COVERAGE_AXES: dict[str, list[str]] = {
    "architecture-decision": ["maintainability", "operability", "security", "performance",
                              "reversibility", "team-fit", "cost"],
    "regulatory": ["jurisdiction", "market", "enforcement", "precedent"],
    "competitive": ["product", "pricing", "moat", "customer", "threat"],
    "risk-assessment": ["technical", "operational", "regulatory", "financial", "reputation"],
}

#: Substring keywords (DE+EN) that mark an axis as covered by a role's text. An axis with no entry
#: falls back to matching its own name as a substring.
_AXIS_KEYWORDS: dict[str, set[str]] = {
    # architecture-decision
    "maintainability": {"maintain", "wart", "erweiter", "verständ", "schuld", "altert", "evolv"},
    "operability": {"sre", "betreib", "observ", "recover", "deployment", "rollback", "monitoring",
                    "reliab", "toil", "failure"},
    "security": {"security", "sicherheit", "zero-trust", "angriff", "secret", "berechtig", "souverän"},
    "performance": {"performance", "durchsatz", "latenz", "ressource", "skalier", "bandbreite",
                    "engpass", "last"},
    "reversibility": {"reversib", "lock-in", "exit", "einbahn", "zweibahn", "zurückrud",
                      "optionswert", "vendor"},
    "team-fit": {"team", "lernkurve", "konvention", "wissens", "passung", "skill"},
    "cost": {"kosten", "tco", "lizenz", "compute", "opportunität", "ownership"},
    # regulatory
    "jurisdiction": {"jurisdik", "regulier", "recht", "freezone", "vae", "statute", "agency"},
    "market": {"markt", "geschäft", "marktdynamik"},
    "enforcement": {"enforcement", "durchsetz", "compliance", "vollz", "strafe", "buße"},
    "precedent": {"präzedenz", "case-law", "rechtsprech", "fälle", "auslegung"},
    # competitive
    "product": {"produkt", "funktionsumfang", "roadmap", "differenz", "ux"},
    "pricing": {"pricing", "gtm", "preis", "packaging", "vertrieb", "kommerz", "go-to-market"},
    "moat": {"moat", "verteidig", "burggräb", "netzwerk", "wechselkost", "vorsprung", "tech-strat"},
    "customer": {"kunde", "use-case", "segment", "jobs-to-be-done"},
    "threat": {"bedrohung", "substitution", "aufklärer", "risiko", "spieler"},
    # risk-assessment
    "technical": {"technisch", "architektur", "skalier", "stabilit", "single-point"},
    "operational": {"operativ", "prozess", "personal", "lieferkett", "abhängig", "betriebskontinu"},
    "regulatory": {"regulator", "compliance", "verstoß", "lizenz", "genehmig"},
    "financial": {"finanz", "cashflow", "wechselkurs", "exposure", "runaway"},
    "reputation": {"reputation", "stakeholder", "wahrnehm", "vertrauen", "eskalation"},
}


def _axis_keywords(axis: str) -> set[str]:
    # Always match the axis's own (English) name too — so an English role label/text (e.g. "Technical",
    # "Market Analyst") covers its axis just as the German keyword set does (the sets are DE+EN, #969).
    return _AXIS_KEYWORDS.get(axis, set()) | {axis}


def axes_covered(role_texts: Iterable[str], axes: Iterable[str]) -> set[str]:
    """Which *axes* are covered by any of *role_texts* (substring keyword match).

    Public so the router's coverage guard (Spec 04 §6.2) reuses the one axis-detection definition on
    its ``Perspective`` texts — same logic ``check_coverage`` runs over a ``Panel``'s role texts.
    """
    covered: set[str] = set()
    blobs = [t.lower() for t in role_texts]
    for axis in axes:
        kws = _axis_keywords(axis)
        if any(kw in blob for blob in blobs for kw in kws):
            covered.add(axis)
    return covered


def _covered_axes(panel: Panel, axes: Iterable[str]) -> set[str]:
    return axes_covered([f"{r.role} {r.lens_prompt}" for r in panel.roles], axes)


def check_coverage(panel: Panel, *, required_axes: Optional[list[str]] = None) -> list[str]:
    """Findings (empty == ok): expected axes the panel does not cover.

    ``required_axes`` defaults to ``COVERAGE_AXES[panel.domain]``; an unknown/adhoc domain has no
    reference axes → no findings (Spec 04 §6.2 / §8: coverage is a no-op for adhoc).
    """
    axes = required_axes if required_axes is not None else COVERAGE_AXES.get(panel.domain, [])
    if not axes:
        return []
    covered = _covered_axes(panel, axes)
    return [f"axis {axis!r} uncovered — add a role" for axis in axes if axis not in covered]
