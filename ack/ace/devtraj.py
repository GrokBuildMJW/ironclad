"""ACE devtraj — the pure dev-process ledger → Trajectory adapter (epic #855 cluster ACE-DEVTRAJ / #877
M4-1, catalogue DP-1).

Variant A (ledger-derived) of the dev-process self-learning integration: the dev-loop records its whole
trajectory — gate pass/fail, guard trips, review verdicts, merge, abort — as plain-data records in the
transition ledger (``<repo>/.devloop/ledger.jsonl``), the SAME ledger the engine's ``lifecycle_projector``
already consumes. This module re-projects ALREADY-PARSED ledger payloads into one ACE :class:`Trajectory`
per unit of work, so ACE can learn from the dev-process itself (which strategies got a unit to the merge
gate, which made it fail a guard).

It is a **pure** function over the dict list ``gx10._read_ledger_payloads`` returns (the same parse +
chain-verify split ``lifecycle_projector`` uses) — stdlib + the sibling :class:`Trajectory` only, importing
**nothing** from the engine / gx10 / the private ``scripts/devprocess`` / ``scripts/devloop`` (clean-room).
The ledger record schema below is the dev-process's public data contract (field names + state strings),
NOT private literals — mirrored from ``lifecycle_projector`` so the two agree.

**Record shapes** (matched, not guessed): a driver transition ``{"unit", "src", "dst", "guard", "passed",
"reasons"}`` (a green composed GATE = ``dst=="GATE", guard=="gate", passed``; an enforced review =
``dst=="REVIEW", guard=="review-evidence", passed`` and NOT ``(inert)``; the per-unit terminal = the
``dst=="MERGE"`` leg — the driver stops at the **human merge gate**, so this is *reached-human-merge-gate*,
NOT a confirmed merge); a DELIVER record ``{"surface":"DELIVER", "state", "status", "reasons"}`` (per-RELEASE,
carries **no** ``unit`` — so it can't be unit-correlated and is not used for a per-unit outcome); an abort
record ``{"abort": <unit>, "reason", ...}``.

**Label-free** (O-001): the outcome is derived from the execution trajectory alone. **Schema-drift tolerant**
+ never raises: a non-dict / missing-key / partial payload degrades to a thinner trajectory; an empty ledger
yields no trajectories. ``used_bullet_ids`` is always ``[]`` here — M4-3 (#880) correlates the per-unit
injected bullets.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .reflector import Trajectory

# ── the dev-process ledger data contract (mirrors engine.lifecycle_projector; NOT an import) ───────────
_GATE_GUARD = "gate"                 # the composed gate-runner's guard name (driver.step records it)
_REVIEW_GUARD = "review-evidence"    # the GATE→REVIEW A↔B-convergence leg's guard name
_INERT_REVIEW_MARKER = "(inert)"     # run.py marks a dry-run / non-enforced review pass with this in `reasons`
_MERGE_DST = "MERGE"                 # the driver's terminal leg = parked at the human merge gate
_REASON_CAP = 160                    # bound a failure reason fed into a step (it can carry log text)


def _payload_of(record: Any) -> "Optional[Dict[str, Any]]":
    """Normalize a ledger element to its payload dict — a full record ``{"seq","prev_hash","payload","hash"}``
    yields its ``payload``; a bare driver/deliver/abort dict is returned as-is; anything else → ``None``."""
    if not isinstance(record, dict):
        return None
    inner = record.get("payload")
    return inner if isinstance(inner, dict) else record


def _leg_summary(p: "Dict[str, Any]") -> str:
    """A stable one-line summary of a driver transition leg, from its identifying fields. A failed leg also
    carries a BOUNDED reason (the failure-diagnosis signal the Reflector learns from, E-001)."""
    src, dst, guard = p.get("src"), p.get("dst"), p.get("guard")
    if p.get("passed"):
        if dst == "GATE" and guard == _GATE_GUARD:
            return "GATE gate passed"
        if dst == "REVIEW" and guard == _REVIEW_GUARD:
            reasons = p.get("reasons") or []
            if any(_INERT_REVIEW_MARKER in str(r) for r in reasons):
                return "REVIEW review-evidence (inert)"
            return "REVIEW review-evidence passed"
        return f"{src}->{dst} {guard} passed"
    reasons = p.get("reasons") or []
    why = "; ".join(str(r) for r in reasons)[:_REASON_CAP] if isinstance(reasons, list) else ""
    return f"{src}->{dst} {guard} FAILED" + (f": {why}" if why else "")


def _new_unit() -> "Dict[str, Any]":
    return {"steps": [], "reached_merge": False, "blocked": False, "aborted": False}


def ledger_to_trajectories(payloads: Any) -> "List[Trajectory]":
    """Project a list of already-parsed ledger payloads into one :class:`Trajectory` per dev-loop unit, in
    first-seen unit order. Driver transitions are grouped by their ``unit``; an abort record by its ``abort``
    key. Per unit: ``query`` = the unit id; ``steps`` = the ordered leg summaries; ``outcome`` (label-free)
    ∈ {``aborted``, ``reached-human-merge-gate``, ``blocked``, ``in-progress``}; ``used_bullet_ids`` = ``[]``
    (M4-3 populates). A DELIVER record (no ``unit``) is skipped (documented granularity). Never raises."""
    try:
        units: "Dict[str, Dict[str, Any]]" = {}    # insertion-ordered → first-seen unit order
        for rec in (payloads or []):
            p = _payload_of(rec)
            if not isinstance(p, dict):
                continue
            abort_unit = p.get("abort")
            if abort_unit is not None:              # abort record: {"abort": <unit>, "reason", ...}
                st = units.setdefault(str(abort_unit), _new_unit())
                st["aborted"] = True
                st["steps"].append(f"abort: {str(p.get('reason') or '')[:_REASON_CAP]}".rstrip(": ").rstrip())
                continue
            unit = p.get("unit")
            if unit is not None and ("dst" in p or "src" in p or "guard" in p):   # a driver transition leg
                st = units.setdefault(str(unit), _new_unit())
                st["steps"].append(_leg_summary(p))
                if p.get("passed") and p.get("dst") == _MERGE_DST:
                    st["reached_merge"] = True
                elif not p.get("passed"):
                    st["blocked"] = True
            # else: a DELIVER record (no unit) or an unrecognized payload → not unit-correlated, skipped
        out: "List[Trajectory]" = []
        for unit, st in units.items():
            if st["aborted"]:
                outcome = "aborted"
            elif st["reached_merge"]:
                outcome = "reached-human-merge-gate"
            elif st["blocked"]:
                outcome = "blocked"
            else:
                outcome = "in-progress"
            out.append(Trajectory(query=unit, steps=list(st["steps"]), outcome=outcome, used_bullet_ids=[]))
        return out
    except Exception:   # noqa: BLE001 — advisory: a malformed ledger must never break the caller
        return []
