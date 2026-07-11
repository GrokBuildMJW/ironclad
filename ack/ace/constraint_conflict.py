"""Structured constraint-conflict detector + L3 hard-check (epic #1344 S3/S6).

Pure, boundary-clean L2/L3 helpers over the typed allow-list:

* :func:`detect_conflict` — advisory L2: first differing shared ``TYPED_KEYS`` entry
  (omission is not a conflict).
* :func:`hardcheck` — fail-closed L3: first HARD-category key that is missing from the
  provided map (when ``require_present``) or present with a mismatched value.

Never raises. No engine import. No persistence / MPR / ``/fork`` surface.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

from .constraint_types import TYPED_KEYS


@dataclass(frozen=True)
class Conflict:
    """A single typed-key mismatch: *required* is the HARD floor, *counter* the design."""

    category: str
    required: Any
    counter: Any


@dataclass(frozen=True)
class Violation:
    """A single L3 hard-check failure against a HARD typed floor.

    *kind* is ``"missing"`` (omission when ``require_present``) or ``"mismatch"``
    (present but unequal). *provided* is ``None`` on a missing violation.
    """

    category: str
    required: Any
    provided: Any
    kind: str  # "missing" | "mismatch"


def detect_conflict(
    constraint_typed: Mapping[str, Any],
    design_typed: Mapping[str, Any],
) -> Optional[Conflict]:
    """Return a :class:`Conflict` iff a typed key is present in BOTH maps with different values.

    Walks ``TYPED_KEYS`` in frozen order (first-match wins). Keys present on only one side
    do not conflict. Empty / non-mapping inputs → ``None``. Pure; never raises.
    """
    try:
        if not isinstance(constraint_typed, Mapping) or not isinstance(design_typed, Mapping):
            return None
        for key in TYPED_KEYS:
            if key not in constraint_typed or key not in design_typed:
                continue
            req = constraint_typed[key]
            ctr = design_typed[key]
            if req != ctr:
                return Conflict(category=key, required=req, counter=ctr)
        return None
    except Exception:  # noqa: BLE001 — pure: hostile mapping never breaks a caller
        return None


def hardcheck(
    constraint_typed: Mapping[str, Any],
    provided_typed: Mapping[str, Any],
    *,
    require_present: bool,
) -> Optional[Violation]:
    """Fail-closed L3 compare of *provided_typed* against the HARD floor *constraint_typed*.

    For each key in ``TYPED_KEYS`` that is present on the HARD floor:

    * if *require_present* and the key is **absent** from *provided_typed* →
      ``Violation(kind="missing", provided=None, ...)``
    * if present and the values differ → ``Violation(kind="mismatch", ...)``

    First match wins. Empty / non-mapping inputs → ``None`` (no HARD floor / no compare).
    Distinct from :func:`detect_conflict`: this also fails on **omission** when required.
    Pure; never raises.
    """
    try:
        if not isinstance(constraint_typed, Mapping) or not isinstance(provided_typed, Mapping):
            return None
        for key in TYPED_KEYS:
            if key not in constraint_typed:
                continue
            req = constraint_typed[key]
            if key not in provided_typed:
                if require_present:
                    return Violation(
                        category=key, required=req, provided=None, kind="missing"
                    )
                continue
            prov = provided_typed[key]
            if req != prov:
                return Violation(
                    category=key, required=req, provided=prov, kind="mismatch"
                )
        return None
    except Exception:  # noqa: BLE001 — pure: hostile mapping never breaks a caller
        return None
