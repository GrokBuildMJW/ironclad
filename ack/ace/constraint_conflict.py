"""Typed build-standard hard-check helper.

S1 (#1414) retired the product constraint-conflict detector and fork envelope
machinery. The remaining public helper is the fail-closed comparator used by
the approved-design build boundary.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional


@dataclass(frozen=True)
class Violation:
    """A single hard-check failure against a required typed field."""

    category: str
    required: Any
    provided: Any
    kind: str  # "missing" | "mismatch"


def hardcheck(
    required_typed: Mapping[str, Any],
    provided_typed: Mapping[str, Any],
    *,
    require_present: bool,
) -> Optional[Violation]:
    """Compare *provided_typed* against *required_typed*.

    First match wins in the required mapping's order. Empty or non-mapping inputs
    return ``None``. Hostile mappings never raise.
    """
    try:
        if not isinstance(required_typed, Mapping) or not isinstance(provided_typed, Mapping):
            return None
        for key, req in required_typed.items():
            if key not in provided_typed:
                if require_present:
                    return Violation(category=str(key), required=req, provided=None, kind="missing")
                continue
            prov = provided_typed[key]
            if req != prov:
                return Violation(category=str(key), required=req, provided=prov, kind="mismatch")
        return None
    except Exception:  # noqa: BLE001
        return None
