"""ACE Curator — integrates Reflector insights into the playbook as compact, DETERMINISTIC delta updates.

Epic #855 cluster ACE-CURATE (catalogue F-001 delta generation, F-002 section assignment, F-003 JSON
`{reasoning, operations}` format, C-001 delta-not-full-rewrite, C-002 localized updates, N-003 Curator input).

ACE's third role: the Reflector (#858) does the LLM analysis + extracts typed insights/ratings; the Curator
turns those into a compact **delta** (a small set of operations) and the delta is **merged deterministically**
into the playbook (the paper's key cost/latency win — no monolithic LLM rewrite, so no context collapse).
Because the Reflector already produced typed output, this Curator is **deterministic** (no second LLM call):
it forms ops from the Reflector output and applies them in place. ``Delta.from_json`` additionally parses an
externally/LLM-emitted delta, so an LLM-driven Curator variant can ride the same merge.

Pure / stdlib-only — imports only the sibling data model. Robust: an op targeting a missing bullet, an
unknown op type, or a bad section is skipped (counted), never raised.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import List, Optional

from .playbook import Bullet, Playbook, DEFAULT_SECTIONS, HELPFUL, HARMFUL, NEUTRAL
from .reflector import ReflectorOutput

#: Delta operation kinds. ADD appends a new bullet; RATE bumps a used bullet's counter (+ tags it); TAG adds
#: a tag; REMOVE deletes a bullet by id (the primitive item-level unlearning Q-001, #866, builds on).
OP_ADD = "add"
OP_RATE = "rate"
OP_TAG = "tag"
OP_REMOVE = "remove"
_OPS = (OP_ADD, OP_RATE, OP_TAG, OP_REMOVE)
_VERDICTS = (HELPFUL, HARMFUL, NEUTRAL)


@dataclass
class DeltaOp:
    """One localized operation (C-002). ADD carries content/section/tags; RATE carries bullet_id/verdict;
    TAG carries bullet_id/tags; REMOVE carries bullet_id."""

    op: str
    section: str = ""
    content: str = ""
    tags: List[str] = field(default_factory=list)
    bullet_id: str = ""
    verdict: str = ""

    def to_dict(self) -> dict:
        return {"op": self.op, "section": self.section, "content": self.content,
                "tags": list(self.tags), "bullet_id": self.bullet_id, "verdict": self.verdict}

    @classmethod
    def from_dict(cls, d: dict) -> "DeltaOp":
        if not isinstance(d, dict):
            raise ValueError("delta op must be an object")
        op = str(d.get("op", "")).strip().lower()
        if op not in _OPS:
            raise ValueError(f"unknown delta op {op!r} (one of {_OPS})")
        return cls(op=op, section=str(d.get("section", "")), content=str(d.get("content", "")),
                   tags=[str(t) for t in (d.get("tags") or [])],
                   bullet_id=str(d.get("bullet_id", "")), verdict=str(d.get("verdict", "")).strip().lower())


@dataclass
class Delta:
    """The Curator output (F-003): a human-readable ``reasoning`` + a list of ``operations``. This is the
    compact delta context that replaces a monolithic rewrite (C-001)."""

    reasoning: str = ""
    operations: List[DeltaOp] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.operations

    def to_dict(self) -> dict:
        return {"reasoning": self.reasoning, "operations": [o.to_dict() for o in self.operations]}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, d: dict) -> "Delta":
        if not isinstance(d, dict):
            raise ValueError("delta must be an object")
        raw = d.get("operations", []) or []
        if not isinstance(raw, list):
            raise ValueError("delta operations must be a list")
        ops: List[DeltaOp] = []
        for o in raw:
            try:
                ops.append(DeltaOp.from_dict(o))
            except ValueError:
                continue                      # drop a malformed op (robust), keep the well-formed ones
        return cls(reasoning=str(d.get("reasoning", "")), operations=ops)

    @classmethod
    def from_json(cls, text: str) -> "Delta":
        try:
            return cls.from_dict(json.loads(text))
        except (json.JSONDecodeError, TypeError) as e:
            raise ValueError(f"delta is not valid JSON: {e}") from e


def curate(reflector_output: ReflectorOutput, *, sections=DEFAULT_SECTIONS) -> Delta:
    """Deterministically synthesize a :class:`Delta` from a Reflector result (N-003 input): each insight →
    an ADD op (its section validated against *sections*, F-002), each rating → a RATE op (C-002 localized).
    Compact (C-001 — ops, never a full rewrite); deterministic (same input → same delta, no LLM)."""
    sections = tuple(sections) or DEFAULT_SECTIONS
    ops: List[DeltaOp] = []
    for ins in reflector_output.insights:
        section = ins.section if ins.section in sections else sections[0]
        ops.append(DeltaOp(op=OP_ADD, section=section, content=ins.content, tags=list(ins.tags)))
    for r in reflector_output.ratings:
        if r.verdict in _VERDICTS:
            ops.append(DeltaOp(op=OP_RATE, bullet_id=r.bullet_id, verdict=r.verdict))
    n_add = sum(1 for o in ops if o.op == OP_ADD)
    n_rate = sum(1 for o in ops if o.op == OP_RATE)
    return Delta(reasoning=f"add {n_add} insight(s); rate {n_rate} used bullet(s)", operations=ops)


def apply_delta(delta: Delta, playbook: Playbook, *, sections=DEFAULT_SECTIONS) -> dict:
    """Merge *delta* into *playbook* IN PLACE, DETERMINISTICALLY (the paper's deterministic merge). Returns a
    summary ``{added, rated, tagged, removed, skipped}``. Robust: an op targeting a missing bullet, an unknown
    op, or (for ADD) an empty content is skipped + counted, never raised. RATE also tags the bullet with its
    verdict (so the helpful/harmful counter B-001 and the tag B-003 stay in sync)."""
    sections = tuple(sections) or DEFAULT_SECTIONS
    summary = {"added": 0, "rated": 0, "tagged": 0, "removed": 0, "skipped": 0}
    for op in delta.operations:
        if op.op == OP_ADD:
            content = (op.content or "").strip()
            if not content:
                summary["skipped"] += 1
                continue
            section = op.section if op.section in sections else sections[0]
            playbook.add_bullet(content, section, tags=op.tags)
            summary["added"] += 1
        elif op.op == OP_RATE:
            b = playbook.get(op.bullet_id)
            if b is None or op.verdict not in _VERDICTS:
                summary["skipped"] += 1
                continue
            if op.verdict == HELPFUL:
                b.mark_helpful()
            elif op.verdict == HARMFUL:
                b.mark_harmful()
            b.add_tag(op.verdict)
            summary["rated"] += 1
        elif op.op == OP_TAG:
            b = playbook.get(op.bullet_id)
            if b is None or not op.tags:
                summary["skipped"] += 1
                continue
            for t in op.tags:
                b.add_tag(t)
            summary["tagged"] += 1
        elif op.op == OP_REMOVE:
            if playbook.remove(op.bullet_id):
                summary["removed"] += 1
            else:
                summary["skipped"] += 1
        else:  # unknown op (shouldn't reach — DeltaOp validates — but stay defensive)
            summary["skipped"] += 1
    return summary
