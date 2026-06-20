"""Shared template helpers (Spec 06 §4) — JSON extraction + conflict-zone rendering, LLM-free.

The synthesis stage emits a ```json block + prose; the validators parse the block against a pydantic
schema and MPR renders the form deterministically. Conflict zones (from conflicts.py, already
blocking-first) are embedded verbatim and NEVER trimmed — the conflict value must not be lost even on a
parse failure.
"""
from __future__ import annotations

import json
import re
from typing import List, Optional

from ..conflicts import Conflict

_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.DOTALL)

_SEV_LABEL = {"blocking": "⚠ blocking", "material": "• material", "minor": "· minor"}


def extract_json(body: str) -> Optional[dict]:
    """Pull the JSON object out of an LLM body (raw, fenced, or embedded). None on failure."""
    s = (body or "").strip()
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except Exception:  # noqa: BLE001
        pass
    m = _FENCE_RE.search(s)
    if m:
        try:
            obj = json.loads(m.group(1))
            if isinstance(obj, dict):
                return obj
        except Exception:  # noqa: BLE001
            pass
    i, j = s.find("{"), s.rfind("}")
    if i != -1 and j > i:
        try:
            obj = json.loads(s[i:j + 1])
            if isinstance(obj, dict):
                return obj
        except Exception:  # noqa: BLE001
            pass
    return None


def conflict_zones_md(conflicts: List[Conflict]) -> str:
    """Render the conflict zones (blocking first, never trimmed). '' when there are none."""
    if not conflicts:
        return ""
    lines = ["### Konfliktzonen"]
    for c in conflicts:
        label = _SEV_LABEL.get(c.severity, c.severity)
        sides = " ↔ ".join(f"[{', '.join(s.roles)}] {s.stance}" for s in c.sides)
        lines.append(f"- {label} [{c.kind}] {c.topic}: {sides}")
    return "\n".join(lines)


def raw_with_conflicts(body: str, conflicts: List[Conflict]) -> str:
    """Best-effort fallback when the JSON cannot be parsed: raw prose + conflict zones preserved."""
    parts = [(body or "").strip()]
    cz = conflict_zones_md(conflicts)
    if cz:
        parts.append(cz)
    return "\n\n".join(p for p in parts if p)


def warnings_block(warnings: List[str]) -> str:
    return "\n".join(f"> ⚠ {w}" for w in warnings)
