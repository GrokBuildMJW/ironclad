"""Durable fork-envelope contract for L2 constraint conflicts (epic #1344 S3 / #1337).

Pure, boundary-clean data model for a pending constraint-conflict fork persisted to the
initiative vault ledger (``proposals/forks/<fork_id>.json``). Opaque stable ``fork_id``
(sha256 prefix) — free-text ``question`` is **excluded** from identity (R4).

**No MPR, no operator surface, no resolution** — recommendation/matrix stay ``None`` until
S4's worker fills them; status defaults to ``pending``. No engine import.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .constraint_conflict import Conflict


def _s(v: Any) -> str:
    """Coerce to a stripped string; None → ''. Never raises."""
    try:
        return "" if v is None else str(v)
    except Exception:  # noqa: BLE001
        return ""


def make_fork_id(
    slug: str,
    category: str,
    constraint_rev: str,
    design_rev: str,
    option_ids: Any,
) -> str:
    """Opaque stable 16-hex identity from slug/category/revs/sorted option ids.

    The free-text question is intentionally excluded so wording tweaks never fork a new
    ledger key. Never raises (hostile inputs coerce).
    """
    try:
        ids: List[str] = []
        if isinstance(option_ids, (list, tuple)):
            for x in option_ids:
                s = _s(x).strip()
                if s:
                    ids.append(s)
        parts = [
            _s(slug),
            _s(category),
            _s(constraint_rev),
            _s(design_rev),
            *sorted(ids),
        ]
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]
    except Exception:  # noqa: BLE001
        return hashlib.sha256(b"").hexdigest()[:16]


@dataclass
class ForkEnvelope:
    """Pending (or later resolved) constraint-conflict fork ledger record.

    The durable "needs MPR" state is ``status == "pending"`` and ``recommendation is None``.
    The S4 MPR run lock is **process-local and non-durable** (engine-side in-memory set keyed
    by ``fork_id``) so a hard crash never leaves the envelope permanently claimed (#17) — a
    fresh process re-drains any still-pending unfilled envelope. ``inflight`` is a **legacy
    field** kept for ledger round-trip of older files; it is **not** an authoritative claim
    and must never gate resubmit/reclaim. Status stays ``pending`` until the operator decides.
    """

    fork_id: str = ""
    mem_ns: str = ""
    slug: str = ""
    area: str = "constraint"
    category: str = ""
    question: str = ""
    options: List[Dict[str, Any]] = field(default_factory=list)
    recommendation: Optional[Dict[str, Any]] = None
    matrix: Optional[Any] = None
    constraint_rev: str = ""
    design_rev: str = ""
    counter_design: Optional[str] = None
    restore_design: Optional[str] = None
    status: str = "pending"
    resolution: Optional[Dict[str, Any]] = None
    inflight: bool = False  # legacy only — never a durable run lock (#1340 / #17)

    def to_dict(self) -> Dict[str, Any]:
        """JSON-serialisable record (lossless for the contract fields)."""
        return {
            "fork_id": self.fork_id,
            "mem_ns": self.mem_ns,
            "slug": self.slug,
            "area": self.area,
            "category": self.category,
            "question": self.question,
            "options": list(self.options),
            "recommendation": self.recommendation,
            "matrix": self.matrix,
            "constraint_rev": self.constraint_rev,
            "design_rev": self.design_rev,
            "counter_design": self.counter_design,
            "restore_design": self.restore_design,
            "status": self.status,
            "resolution": self.resolution,
            "inflight": bool(self.inflight),
        }

    @classmethod
    def from_dict(cls, d: Any) -> "ForkEnvelope":
        """Drift-tolerant rebuild; missing keys default; extra keys ignored. Never raises."""
        if not isinstance(d, dict):
            return cls()
        opts_raw = d.get("options")
        options: List[Dict[str, Any]] = []
        if isinstance(opts_raw, (list, tuple)):
            for item in opts_raw:
                if isinstance(item, dict):
                    options.append(dict(item))
        rec = d.get("recommendation")
        res = d.get("resolution")
        inflight = d.get("inflight")
        return cls(
            fork_id=_s(d.get("fork_id")).strip(),
            mem_ns=_s(d.get("mem_ns")).strip(),
            slug=_s(d.get("slug")).strip(),
            area=_s(d.get("area")).strip() or "constraint",
            category=_s(d.get("category")).strip(),
            question=_s(d.get("question")).strip(),
            options=options,
            recommendation=rec if isinstance(rec, dict) else None,
            matrix=d.get("matrix"),
            constraint_rev=_s(d.get("constraint_rev")).strip(),
            design_rev=_s(d.get("design_rev")).strip(),
            counter_design=d.get("counter_design") if isinstance(d.get("counter_design"), str) else None,
            restore_design=d.get("restore_design") if isinstance(d.get("restore_design"), str) else None,
            status=_s(d.get("status")).strip() or "pending",
            resolution=res if isinstance(res, dict) else None,
            inflight=bool(inflight) if inflight is not None else False,
        )


def build_constraint_envelope(
    *,
    mem_ns: str,
    slug: str,
    conflict: Conflict,
    constraint_rev: str,
    design_rev: str,
    counter_design: Optional[str] = None,
    restore_design: Optional[str] = None,
) -> ForkEnvelope:
    """Build a pending constraint-area envelope from a detected :class:`Conflict`.

    Options are fixed keep/counter with the required vs proposed typed values.
    ``recommendation`` / ``matrix`` stay ``None`` (S4 MPR worker fills them).
    """
    options = [
        {
            "id": "keep",
            "label": f"keep {conflict.required}",
            "value": conflict.required,
        },
        {
            "id": "counter",
            "label": f"adopt {conflict.counter}",
            "value": conflict.counter,
        },
    ]
    question = (
        f"required {conflict.category}={conflict.required} "
        f"vs proposed {conflict.counter}"
    )
    fork_id = make_fork_id(
        slug,
        conflict.category,
        constraint_rev,
        design_rev,
        [o["id"] for o in options],
    )
    return ForkEnvelope(
        fork_id=fork_id,
        mem_ns=_s(mem_ns).strip(),
        slug=_s(slug).strip(),
        area="constraint",
        category=_s(conflict.category).strip(),
        question=question,
        options=options,
        recommendation=None,
        matrix=None,
        constraint_rev=_s(constraint_rev).strip(),
        design_rev=_s(design_rev).strip(),
        counter_design=counter_design,
        restore_design=restore_design,
        status="pending",
        resolution=None,
    )
