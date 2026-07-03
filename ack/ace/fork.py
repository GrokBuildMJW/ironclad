"""ACE fork signal — the pure, boundary-clean data contract for an architecture DECISION FORK in the
dev-process (epic #855 cluster ACE-FORKSIG / #882, catalogue MPR-A-1).

M5 is the *propose* layer of the ACE loop: at an architecture fork, MPR produces a multi-perspective
decision-matrix as a well-founded proposal; ACE records the decision + its outcome so the next comparable
fork is pre-informed. This module is the FOUNDATION everything M5 rides — the structured **ForkSignal** (a
declared fork: a question + candidate options + the touched paths) and **ForkResolution** (the chosen option
+ its later outcome), plus a PURE adapter that reads them off the same boundary-clean dev-loop ledger/artifact
seam `lifecycle_projector` + `ack.ace.devtraj` (M4-1) consume — as plain data.

**Fork-detection = Variant A (declared fork, resolved M5 C0):** the fork is recognized at the dev-loop's
existing STOP-and-ask point — the build agent / operator, on ambiguity, emits a `ForkSignal` marker
(`{"surface": "FORK", unit, area, question, options, touched_paths}`) into the ledger. Reversible: a future
Variant-B auto-detector reuses this exact schema. No engine wiring / no MPR call / no proposal here (those are
M5-2..M5-5).

**Pure / stdlib-only** — imports nothing from the engine / gx10 / the private `scripts/devloop` /
`scripts/devprocess`. **Drift-tolerant + never raises**: a partial / extra-field / garbage payload degrades to
a thinner `ForkSignal` (or is skipped), matching `devtraj`'s conservative parse.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

#: the ledger/artifact surface markers a declared fork + its resolution carry (the data contract; NOT a
#: private literal — a schema string, like devtraj's `MERGE`/`DELIVER`).
FORK_SURFACE = "FORK"
FORK_RESOLVED_SURFACE = "FORK_RESOLVED"


def _s(v: Any) -> str:
    """Coerce to a stripped string; None → ''. Never raises."""
    try:
        return "" if v is None else str(v)
    except Exception:   # noqa: BLE001 — a hostile __str__ never breaks a parse
        return ""


def _slist(v: Any) -> "List[str]":
    """Coerce to a list of non-empty strings; a non-list / hostile value → []. Never raises."""
    try:
        if not isinstance(v, (list, tuple)):
            return []
        out: "List[str]" = []
        for x in v:
            s = _s(x).strip()
            if s:
                out.append(s)
        return out
    except Exception:   # noqa: BLE001
        return []


@dataclass
class ForkSignal:
    """A declared architecture fork (MPR-A-1): the *unit* (dev-loop issue#) it arose in, the *area* it
    concerns, the *question*, the candidate *options*, and the *touched_paths*. All fields optional/defaulted
    so a partial record still yields a usable (thinner) signal."""

    unit: str = ""
    area: str = ""
    question: str = ""
    options: "List[str]" = field(default_factory=list)
    touched_paths: "List[str]" = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"surface": FORK_SURFACE, "unit": self.unit, "area": self.area, "question": self.question,
                "options": list(self.options), "touched_paths": list(self.touched_paths)}

    @classmethod
    def from_dict(cls, d: Any) -> "ForkSignal":
        """Drift-tolerant: missing keys default, extra keys ignored, types coerced. Never raises."""
        if not isinstance(d, dict):
            return cls()
        return cls(unit=_s(d.get("unit")).strip(), area=_s(d.get("area")).strip(),
                   question=_s(d.get("question")).strip(), options=_slist(d.get("options")),
                   touched_paths=_slist(d.get("touched_paths")))

    def is_empty(self) -> bool:
        return not (self.unit or self.question or self.options)


# ── #1066: Variant-B ambiguity auto-detector ──────────────────────────────────────────────────────────
# The no-guessing rule is a PROMPT CONVENTION today (Variant A relies on the agent/operator NOTICING the
# ambiguity and declaring a ForkSignal). Variant B is the safety net for the autonomous case: a pure,
# deterministic pre-flight scan that flags requirement underspecification / ambiguity and emits the SAME
# ForkSignal — so an agent that does NOT notice the ambiguity is stopped (halt-to-ask), not left to guess.
# Precision-first (it must not cry wolf): only clear signals fire.
_AMBIGUITY_MARKERS = (
    "tbd", "to be decided", "to be determined", "not sure", "unclear", "unspecified",
    "figure out", "figure it out", "somehow", "or something", "???",
)
_AMBIGUITY_PATTERNS = (
    (re.compile(r"\b(either|and/or|or should|whichever|one of|any of)\b", re.I), "multiple interpretations (either/or)"),
    (re.compile(r"\b(appropriately|as appropriate|as needed|as required|etc\.?|and so on|properly|reasonably)\b", re.I),
     "vague qualifier (appropriately/as needed/etc.)"),
    (re.compile(r"\b(but not|however not|except maybe|not sure (if|whether))\b", re.I), "internal contradiction / hedge"),
)


def ambiguity_signals(text: str) -> "List[str]":
    """The concrete ambiguity/underspecification signals in *text* (empty ⇒ reads unambiguous). Pure,
    precision-first: an explicit uncertainty marker, an open question posed inside the requirement, a
    multiple-interpretation phrase, a vague qualifier, or an internal hedge/contradiction."""
    t = (text or "").strip()
    if not t:
        return []
    low = t.lower()
    signals: "List[str]" = []
    for m in _AMBIGUITY_MARKERS:
        if m in low:
            signals.append(f"uncertainty marker: '{m}'")
    if "?" in t:
        signals.append("contains an open question ('?')")
    for rx, label in _AMBIGUITY_PATTERNS:
        if rx.search(low):
            signals.append(label)
    # de-dupe preserving order
    seen: "set" = set()
    return [s for s in signals if not (s in seen or seen.add(s))]


def detect_ambiguity(text: str, *, unit: str = "", area: str = "requirements") -> "Optional[ForkSignal]":
    """Variant-B auto-detector: if *text* carries ambiguity/underspecification signals, return a ForkSignal
    (halt-to-ask) reusing the Variant-A schema; else None. Never raises. The engine gates whether a positive
    result HALTS or merely warns (default-off) — this function only detects."""
    try:
        signals = ambiguity_signals(text)
    except Exception:   # noqa: BLE001 — detection must never raise into the loop
        return None
    if not signals:
        return None
    question = "Ambiguity detected — clarify before building: " + "; ".join(signals[:4])
    options = ["Ask the operator to specify the missing detail",
               "Proceed with an explicitly stated assumption (record it)"]
    return ForkSignal(unit=unit, area=area, question=question, options=options)


@dataclass
class ForkResolution:
    """An operator's resolution of a fork: the *chosen_option* and (once the unit reaches DELIVER/abort) its
    *outcome*. Keyed by *unit* + *area* so the next comparable fork can retrieve the prior decision."""

    unit: str = ""
    area: str = ""
    chosen_option: str = ""
    outcome: str = ""

    def to_dict(self) -> dict:
        return {"surface": FORK_RESOLVED_SURFACE, "unit": self.unit, "area": self.area,
                "chosen_option": self.chosen_option, "outcome": self.outcome}

    @classmethod
    def from_dict(cls, d: Any) -> "ForkResolution":
        if not isinstance(d, dict):
            return cls()
        return cls(unit=_s(d.get("unit")).strip(), area=_s(d.get("area")).strip(),
                   chosen_option=_s(d.get("chosen_option")).strip(), outcome=_s(d.get("outcome")).strip())

    def is_empty(self) -> bool:
        return not (self.unit or self.chosen_option)


def _payload_of(record: Any) -> "Optional[Dict[str, Any]]":
    """Normalize a ledger element to its payload dict — a full record `{seq,prev_hash,payload,hash}` yields
    its `payload`; a bare dict is returned as-is; anything else → None. (Mirrors `devtraj._payload_of`.)"""
    if not isinstance(record, dict):
        return None
    inner = record.get("payload")
    return inner if isinstance(inner, dict) else record


def parse_fork_signal(payload: Any) -> "Optional[ForkSignal]":
    """A `ForkSignal` from ONE payload iff it is a FORK-surface record (else None). Never raises."""
    p = _payload_of(payload)
    if not isinstance(p, dict) or p.get("surface") != FORK_SURFACE:
        return None
    sig = ForkSignal.from_dict(p)
    return sig if not sig.is_empty() else None


def parse_fork_resolution(payload: Any) -> "Optional[ForkResolution]":
    """A `ForkResolution` from ONE payload iff it is a FORK_RESOLVED-surface record (else None). Never raises."""
    p = _payload_of(payload)
    if not isinstance(p, dict) or p.get("surface") != FORK_RESOLVED_SURFACE:
        return None
    res = ForkResolution.from_dict(p)
    return res if not res.is_empty() else None


def fork_signals_from(payloads: Any) -> "List[ForkSignal]":
    """Every declared `ForkSignal` in *payloads*, in order. Drift-tolerant; never raises."""
    try:
        return [s for s in (parse_fork_signal(p) for p in (payloads or [])) if s is not None]
    except Exception:   # noqa: BLE001 — advisory: a malformed ledger never breaks the caller
        return []


def fork_resolutions_from(payloads: Any) -> "List[ForkResolution]":
    """Every `ForkResolution` in *payloads*, in order. Drift-tolerant; never raises."""
    try:
        return [r for r in (parse_fork_resolution(p) for p in (payloads or [])) if r is not None]
    except Exception:   # noqa: BLE001
        return []
