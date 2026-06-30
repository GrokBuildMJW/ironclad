"""Pure lifecycle-evidence projector (S13b / AD-7) — the engine DELIVER-leg's bridge from the
dev-process transition ledger to stage-tagged vault evidence + the completeness gate.

This module is PURE: stdlib only; it imports nothing from ``gx10`` and nothing from
``scripts/devprocess`` / ``scripts/devloop`` (the core/ boundary forbids reaching into the private
bare-runner modules — the ledger is consumed as plain data). The two engine primitives it needs —
``project_evidence`` and ``lifecycle_completeness`` — are **injected** by the caller (gx10's
``/lifecycle`` command wires the real ones), so the mapper + projector stay unit-testable with fakes
and boundary-clean.

The transition->stage map (Fork B) is derived from the REAL driver/deliver payload shapes (matched
exactly, never guessed):

- ``scripts/devprocess/driver.py`` (SSOT; ``scripts/devloop/driver.py`` re-exports it) emits one
  record per transition: ``{"unit", "src", "dst", "guard", "passed", "reasons"}``. A **green composed
  GATE** leg — ``dst == "GATE"`` with ``guard == "gate"`` (the name the wired gate-runner composes via
  ``guards.compose("gate", ...)`` in ``scripts/devprocess/e2e.py`` / ``shell_guard("gate", ...)``) and
  ``passed`` true — maps to the ``tests`` stage. A **green review-evidence** leg — ``dst == "REVIEW"``
  with ``guard == "review-evidence"`` and ``passed`` true (the GATE->REVIEW A<->B-convergence
  transition, ``run.py``) — maps to the ``reviews`` stage, **unless** it is a dry-run / non-live review
  (``run.py`` marks such a leg with the ``(inert)`` sentinel in its ``reasons``), which is excluded so the
  ``reviews`` stage requires an ENFORCED review (#830). (The other GATE->GATE legs carry
  ``guard == "coupling"`` / ``"apply"``, so they never map to ``tests``.)
- ``scripts/devloop/deliver.py`` emits ``{"surface": "DELIVER", "state", "status", "reasons"}``. A
  record whose ``status`` shows the irreversible push fired — ``delivered`` / ``delivered-pending`` /
  ``delivered-unrecorded`` — maps to the ``delivery`` stage. The halted/parked statuses
  (``halted-gate`` / ``parked-awaiting-go`` / ``halted-execute`` / ``halted-error``) did NOT deliver
  and map to nothing.

Everything else maps to ``None`` (skipped). Conservative + deterministic: only a GREEN / delivered
leg is evidence; a non-dict / missing-key payload is crash-safely ignored.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

#: The composed GATE guard name the WIRED gate-runner emits — ``guards.compose("gate", ...)`` /
#: ``shell_guard("gate", ...)`` (``scripts/devprocess/e2e.py``), recorded as ``guard`` by ``driver.step``.
#: A green ``dst == "GATE"`` leg with this guard is the ``tests`` evidence (the co-located ``coupling`` /
#: ``apply`` legs carry other guard names, so they are excluded). NOTE: ``gate.py::composed_gate`` builds a
#: ``"composed-gate"``-named guard, but that helper is NOT wired as the gate-runner — the live name is ``"gate"``.
_GATE_GUARD = "gate"
#: The GATE->REVIEW (A<->B convergence) guard name — ``run.py`` records the review leg as
#: ``GuardResult("review-evidence", ...)``; a green leg with this guard maps to the ``reviews`` stage.
_REVIEW_GUARD = "review-evidence"
#: #830: a DRY-RUN / non-live review leg is recorded PASS-but-INERT — ``run.py`` puts this sentinel token
#: in the leg's ``reasons`` (it emits ``"dry-run: review-evidence not enforced (inert)"``). A pass alone is
#: not real reviews evidence, so the projector keys on this marker to EXCLUDE an inert review from the
#: ``reviews`` stage — reviews evidence requires an ENFORCED (live) review.
_INERT_REVIEW_MARKER = "(inert)"
#: The DELIVER statuses (``scripts/devloop/deliver.py`` / ``run.py``) that mean the irreversible push
#: fired — so a ``delivery``-stage evidence is warranted. Halted/parked statuses are NOT delivered.
_DELIVERED_STATUSES = frozenset({"delivered", "delivered-pending", "delivered-unrecorded"})


def stage_for_payload(payload: Any) -> Optional[str]:
    """Map ONE ledger payload (a driver transition record or a DELIVER record) to its lifecycle stage,
    or ``None`` when the payload is not stage-bearing. Pure, deterministic and crash-safe on garbage
    (a non-dict / missing key yields ``None``). Conservative: only a GREEN driver leg or a delivered
    DELIVER record maps."""
    if not isinstance(payload, dict):
        return None
    # DELIVER surface record (deliver.py emit): map only when the irreversible push actually fired.
    if payload.get("surface") == "DELIVER":
        return "delivery" if payload.get("status") in _DELIVERED_STATUSES else None
    # Driver transition record (driver.py step): only a GREEN leg is evidence.
    if not payload.get("passed"):
        return None
    dst, guard = payload.get("dst"), payload.get("guard")
    if dst == "GATE" and guard == _GATE_GUARD:
        return "tests"      # the composed GATE went green
    if dst == "REVIEW" and guard == _REVIEW_GUARD:
        # #830: a dry-run / non-live review passes but is INERT (run.py marks it with `(inert)`); it is
        # NOT real reviews evidence, so it maps to nothing — the `reviews` stage requires an enforced review.
        reasons = payload.get("reasons") or []
        if any(_INERT_REVIEW_MARKER in str(r) for r in reasons):
            return None
        return "reviews"    # an ENFORCED review-evidence (A<->B convergence) leg passed
    return None


def _payload_of(record: Any) -> Optional[Dict[str, Any]]:
    """Normalize a ledger element to its payload dict. Accepts EITHER a full ledger record
    (``{"seq", "prev_hash", "payload", "hash"}`` — return its ``payload``) OR a bare payload dict
    (driver/deliver records carry no ``payload`` key — return it as-is). Anything else -> ``None``."""
    if not isinstance(record, dict):
        return None
    inner = record.get("payload")
    return inner if isinstance(inner, dict) else record


def _summary_line(payload: Dict[str, Any]) -> str:
    """A STABLE, timestamp-free one-line summary of a stage-bearing payload. Built from the identifying
    fields only (never the ``reasons`` list, which can carry volatile shas / log text) so re-projecting
    the same ledger yields byte-identical evidence (idempotent)."""
    if payload.get("surface") == "DELIVER":
        return f"deliver state={payload.get('state')} status={payload.get('status')}"
    return ("transition"
            f" unit={payload.get('unit')}"
            f" src={payload.get('src')}"
            f" dst={payload.get('dst')}"
            f" guard={payload.get('guard')}"
            f" passed={payload.get('passed')}")


def project_transitions(records: Any, *, slug: str, tree_sha: str, required_stages: List[str],
                        project_evidence: Callable[..., str],
                        lifecycle_completeness: Callable[..., Any]) -> Dict[str, Any]:
    """Project the stage-bearing *records* into vault evidence bound to the delivery *tree_sha*
    (Fork E1: the SAME tree_sha the gate verifies, so the gate passes by construction), then run the
    completeness gate.

    For each stage present in the mapped records, compose ONE deterministic, timestamp-free, sorted +
    deduped summary of that stage's matching payloads and call the injected
    ``project_evidence(stage, title, body, tree_sha=tree_sha, slug=slug)``; then call the injected
    ``lifecycle_completeness(slug, required_stages=required_stages, tree_sha=tree_sha)``. Returns
    ``{"projected": [paths], "ready": bool, "reasons": [...]}``.

    Idempotent: the same records + tree_sha yield byte-identical evidence (project_evidence is a no-op
    re-write), so re-projection adds no files and the ``projected`` paths are stable. Never raises on an
    empty / garbage record list — unmappable elements are skipped. Fail-closed: when *tree_sha* is
    empty no evidence is projected (project_evidence would refuse an unbound write) and the gate's own
    BLOCKED reason is returned instead."""
    by_stage: Dict[str, List[str]] = {}
    for record in (records or []):
        payload = _payload_of(record)
        if payload is None:
            continue
        stage = stage_for_payload(payload)
        if stage is None:
            continue
        by_stage.setdefault(stage, []).append(_summary_line(payload))

    projected: List[str] = []
    # Only bind+write evidence when the delivery tree_sha is present; an empty/blank tree_sha flows
    # through to the gate, which fails closed with its own "no delivery tree_sha" reason.
    if isinstance(tree_sha, str) and tree_sha.strip():
        for stage in sorted(by_stage):
            lines = sorted(set(by_stage[stage]))
            title = f"{stage} evidence"
            body = "\n".join(lines)
            projected.append(project_evidence(stage, title, body, tree_sha=tree_sha, slug=slug))

    ready, reasons = lifecycle_completeness(slug, required_stages=required_stages, tree_sha=tree_sha)
    return {"projected": sorted(projected), "ready": bool(ready), "reasons": list(reasons)}
