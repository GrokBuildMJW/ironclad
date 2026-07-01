"""ACE robustness — graceful degradation, noise tolerance, contradiction handling, selective unlearning,
and playbook versioning/rollback.

Epic #855 cluster ACE-ROBUST (catalogue K-001 weak-reflector graceful degradation, K-002 noisy-update
tolerance, K-003 contradiction detection + resolution, Q-001 selective item-level unlearning, M-002 context
versioning). These are the safety/robustness mechanisms layered on the playbook so a real, imperfect
deployment (a weak reflector, noisy/adversarial updates, accumulating contradictions, a regretted lesson)
degrades gracefully instead of collapsing — and is reversible.

The unifying design that delivers K-001 + K-002: every update is a SMALL, utility-counted delta, so a weak
or noisy reflector's bad bullets accumulate harmful marks and are **quarantined / pruned** while its
occasional good bullet survives — a strong reflector simply contributes more good bullets. `quarantine_noisy`
is that rejection mechanism; `adaptation_gain` quantifies the net useful signal (the monotonicity weak ≤
strong, both > 0, is the K-001 property). `detect_contradictions` + `resolve_contradictions` handle K-003;
`unlearn` is the item-level Q-001 forget (no retraining); `PlaybookHistory` + `version_id` are the M-002
versioning + rollback.

Pure / stdlib-only — operates on the in-memory `Playbook`; never raises (advisory robustness)."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .playbook import Playbook, Bullet

_WORD_RE = re.compile(r"[a-z0-9]+")
#: Negation/polarity markers — a contradiction is two near-identical bullets where exactly one is negated.
#: #905: the bare modals "should"/"must" are NOT negations (they are obligation, not polarity) — including
#: them made "you should X" vs "you X" a false contradiction. Only their negative forms count.
_NEGATIONS = {"not", "no", "never", "dont", "don", "avoid", "without", "cannot", "cant",
              "shouldnt", "mustnt", "skip", "disable"}
#: Default Jaccard overlap (on non-negation tokens) above which two same-section bullets are "about the same".
DEFAULT_CONTRADICTION_OVERLAP = 0.6


def _tokens(text: str) -> set:
    return set(_WORD_RE.findall((text or "").lower()))


# ─── K-001 / K-002: graceful degradation + noisy-update tolerance ─────────────────────────────────────
def adaptation_gain(playbook: Playbook) -> int:
    """The net useful signal accumulated in *playbook* = Σ ``net_utility`` over its bullets (helpful −
    harmful). The K-001 property is demonstrated by this being **positive after a noisy (weak-reflector)
    adaptation + quarantine** and **monotonic** in reflector strength (weak ≤ strong). Never raises."""
    try:
        return sum(b.net_utility for b in playbook.bullets())
    except Exception:  # noqa: BLE001 — advisory metric
        return 0


def quarantine_noisy(playbook: Playbook, *, min_net: int = 0) -> dict:
    """K-002: remove bullets whose ``net_utility`` is **below** *min_net* — i.e. harmful-dominant ones a
    noisy/adversarial reflector produced (graceful: a moderate amount of noise is rejected, the good bullets
    stay, so performance degrades gradually rather than collapsing). ``min_net=0`` drops only net-negative
    bullets; raise it to be stricter. Returns ``{removed, kept}``. Never raises."""
    try:
        doomed = [b.id for b in playbook.bullets() if b.net_utility < min_net]
        for bid in doomed:
            playbook.remove(bid)
        return {"removed": len(doomed), "kept": len(playbook)}
    except Exception:  # noqa: BLE001 — advisory
        return {"removed": 0, "kept": len(playbook)}


# ─── K-003: contradiction detection + resolution ─────────────────────────────────────────────────────
def detect_contradictions(playbook: Playbook) -> "List[Tuple[str, str]]":
    """K-003: find pairs of bullets **in the same section** that are about the same thing (token-Jaccard ≥
    :data:`DEFAULT_CONTRADICTION_OVERLAP` on their non-negation tokens) but of **opposite polarity** (exactly
    one carries a negation marker). Returns a list of ``(id_a, id_b)`` conflict pairs (deterministic order).
    Never raises."""
    pairs: List[Tuple[str, str]] = []
    try:
        for bullets in playbook.sections.values():
            for i in range(len(bullets)):
                for j in range(i + 1, len(bullets)):
                    a, b = bullets[i], bullets[j]
                    ta, tb = _tokens(a.content), _tokens(b.content)
                    neg_a, neg_b = bool(ta & _NEGATIONS), bool(tb & _NEGATIONS)
                    if neg_a == neg_b:
                        continue                                   # same polarity ⇒ not a contradiction
                    core_a, core_b = ta - _NEGATIONS, tb - _NEGATIONS
                    union = core_a | core_b
                    if not union:
                        continue
                    if len(core_a & core_b) / len(union) >= DEFAULT_CONTRADICTION_OVERLAP:
                        pairs.append((a.id, b.id))
    except Exception:  # noqa: BLE001 — advisory
        return pairs
    return pairs


def resolve_contradictions(playbook: Playbook, *, conflicts: "Optional[List[Tuple[str, str]]]" = None) -> dict:
    """K-003: resolve each conflict by KEEPING the more-trusted bullet (higher ``net_utility``; the
    earlier-inserted one wins a tie) and removing the other — so a stale belief that has been contradicted
    by reinforced experience is dropped. Returns ``{resolved, removed_ids}``. Never raises."""
    removed: List[str] = []
    try:
        for id_a, id_b in (conflicts if conflicts is not None else detect_contradictions(playbook)):
            a, b = playbook.get(id_a), playbook.get(id_b)
            if a is None or b is None:
                continue                                           # already resolved by an earlier pair
            loser = b if a.net_utility >= b.net_utility else a     # keep the higher-utility belief
            if playbook.remove(loser.id):
                removed.append(loser.id)
    except Exception:  # noqa: BLE001 — advisory
        pass
    return {"resolved": len(removed), "removed_ids": removed}


# ─── Q-001: selective item-level unlearning ──────────────────────────────────────────────────────────
def unlearn(playbook: Playbook, bullet_ids) -> dict:
    """Q-001: selectively remove individual bullets **by id** — the item-level forget (no retraining, no
    scope-wide wipe). Accepts one id or an iterable. Returns ``{removed, missing}``. Never raises."""
    ids = [bullet_ids] if isinstance(bullet_ids, str) else list(bullet_ids or [])
    removed = 0
    missing: List[str] = []
    for bid in ids:
        try:
            if playbook.remove(bid):
                removed += 1
            else:
                missing.append(bid)
        except Exception:  # noqa: BLE001 — advisory
            missing.append(bid)
    return {"removed": removed, "missing": missing}


# ─── M-002: context versioning + rollback ────────────────────────────────────────────────────────────
def version_id(playbook: Playbook) -> str:
    """M-002: a deterministic content version id for *playbook* — a short hash of its canonical JSON, so any
    two identical playbooks share an id and any change yields a new one (each version is identifiable). ``""``
    only on a serialization failure (never raises)."""
    try:
        return hashlib.sha256(playbook.to_json().encode("utf-8")).hexdigest()[:16]
    except Exception:  # noqa: BLE001 — advisory
        return ""


def diff_versions(before: Playbook, after: Playbook) -> dict:
    """M-002: the traceable change between two playbook states — the added / removed bullet ids and the net
    size delta (so a version's changes are auditable). Never raises."""
    try:
        ids_before = {b.id for b in before.bullets()}
        ids_after = {b.id for b in after.bullets()}
        return {"added": sorted(ids_after - ids_before), "removed": sorted(ids_before - ids_after),
                "size_before": len(before), "size_after": len(after)}
    except Exception:  # noqa: BLE001 — advisory
        return {"added": [], "removed": [], "size_before": 0, "size_after": 0}


@dataclass
class PlaybookHistory:
    """M-002: an ordered version log of playbook snapshots, each identified by its :func:`version_id` — so a
    regretted adaptation can be **rolled back** to a known-good version (the operator-facing safety net the
    item-level `unlearn` complements). Snapshots store the canonical JSON (lossless); `rollback` reconstructs
    a fresh `Playbook`. Stdlib-only, never raises."""

    _log: "List[Tuple[str, str]]" = field(default_factory=list)   # (version_id, json) in commit order

    def snapshot(self, playbook: Playbook) -> str:
        """Record the current state; returns its version id. A no-op (returns the same id) if the tip already
        matches (idempotent)."""
        vid = version_id(playbook)
        try:
            payload = playbook.to_json()
        except Exception:  # noqa: BLE001
            return vid
        if not self._log or self._log[-1][0] != vid:
            self._log.append((vid, payload))
        return vid

    def versions(self) -> List[str]:
        return [v for v, _ in self._log]

    def rollback(self, target: "Optional[str]" = None) -> "Optional[Playbook]":
        """Reconstruct the playbook at *target* version (default: the previous version — undo the last
        snapshot). Returns a fresh `Playbook`, or ``None`` if the target/previous is unavailable."""
        try:
            if target is None:
                if len(self._log) < 2:
                    return None
                payload = self._log[-2][1]
            else:
                match = [j for v, j in self._log if v == target]
                if not match:
                    return None
                payload = match[-1]
            return Playbook.from_json(payload)
        except Exception:  # noqa: BLE001 — a corrupt snapshot → no rollback, never raises
            return None
