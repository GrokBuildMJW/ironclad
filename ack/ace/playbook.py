"""ACE evolving-context playbook — the itemized Bullet + sectioned Playbook data model.

Epic #855 (Loop Intelligence → ACE), cluster ACE-DATA: catalogue requirements B-001 (itemized bullet
structure), B-002 (sectioned playbook structure), B-003 (bullet tagging), M-001 (versioned, round-trippable
storage format). ACE represents context as a collection of structured, itemized **bullets** inside a sectioned
**playbook**, rather than one monolithic prompt — so updates are localized, retrieval is fine-grained, and
grow-and-refine (dedup / prune, ACE-GROW #860) can act per bullet without rewriting the whole context.

This module is the PURE data model: stdlib only (``dataclasses`` + ``json``), imports nothing from the engine
(``ack`` stays dependency-inverted / clean-room-safe). The engine-side persistence + the #602 lesson-backend
migration ride this model in ACE-WIRE (#863); the Reflector/Curator (#858/#859) produce/consume Bullets.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional

#: Storage-format version (M-001). Bumped only on an incompatible on-disk change; ``from_json`` refuses a
#: newer version fail-closed (rollback/migration across versions is ACE-ROBUST #866 / M-002).
SCHEMA_VERSION = 1

#: The four canonical ACE playbook sections (the paper's structure). A Playbook MAY also carry custom
#: sections — the set is open; these are the well-known defaults a Curator (#859) assigns deltas into.
DEFAULT_SECTIONS = (
    "strategies_and_hard_rules",
    "apis_to_use",
    "verification_checklist",
    "formulas_and_calculations",
)

#: Canonical bullet tags the Reflector (#858) attaches (B-003). Free-form tags are allowed too; these are the
#: well-known categorical labels used for utility decisions (e.g. pruning in ACE-GROW).
HELPFUL = "helpful"
HARMFUL = "harmful"
NEUTRAL = "neutral"

_PLAYBOOK_BEGIN = "=== PLAYBOOK (ACE) BEGIN ==="
_PLAYBOOK_END = "=== PLAYBOOK (ACE) END ==="


@dataclass
class Bullet:
    """One itemized context entry (B-001): a stable unique ``id``, the ``content`` (a reusable strategy,
    domain concept, or failure mode), its ``section``, usage counters tracking how often it was marked
    helpful/harmful (the signal grow-and-refine prunes on), and categorical ``tags`` (B-003).

    The id is minted by the owning :class:`Playbook` (``Playbook.add_bullet``) and is stable across
    serialization round-trips, so the Generator can reference *which* bullets it used (H-002, #861) and the
    Curator can target a *specific* bullet for a localized update (C-002, #859)."""

    id: str
    content: str
    section: str
    helpful_count: int = 0
    harmful_count: int = 0
    tags: List[str] = field(default_factory=list)

    def mark_helpful(self) -> None:
        self.helpful_count += 1

    def mark_harmful(self) -> None:
        self.harmful_count += 1

    def add_tag(self, tag: str) -> None:
        """Attach a categorical tag (insertion-ordered, de-duplicated). Empty/blank tags are ignored."""
        t = (tag or "").strip()
        if t and t not in self.tags:
            self.tags.append(t)

    @property
    def net_utility(self) -> int:
        """helpful − harmful — the per-bullet utility score ACE-GROW prunes on (low-utility first)."""
        return self.helpful_count - self.harmful_count

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "content": self.content,
            "section": self.section,
            "helpful_count": int(self.helpful_count),
            "harmful_count": int(self.harmful_count),
            "tags": list(self.tags),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Bullet":
        if not isinstance(d, dict):
            raise ValueError(f"bullet must be an object, got {type(d).__name__}")
        for key in ("id", "content", "section"):
            if not isinstance(d.get(key), str) or not d.get(key):
                raise ValueError(f"bullet missing required string field {key!r}")
        tags = d.get("tags", [])
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            raise ValueError("bullet tags must be a list of strings")
        return cls(
            id=d["id"],
            content=d["content"],
            section=d["section"],
            helpful_count=int(d.get("helpful_count", 0) or 0),
            harmful_count=int(d.get("harmful_count", 0) or 0),
            tags=list(tags),
        )


@dataclass
class Playbook:
    """A sectioned collection of itemized :class:`Bullet`s (B-002) — the evolving context ACE refines.

    Bullets live under named sections (insertion-ordered within a section); ids are minted from a monotonic
    counter that is itself serialized, so ids never collide across a round-trip or after a reload. The model
    is mutable + in-place (ACE grow-and-refine appends new-id bullets and updates existing ones in place);
    serialization is versioned + lossless (M-001)."""

    sections: Dict[str, List[Bullet]] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION
    _seq: int = 0

    # ── construction / mutation ────────────────────────────────────────────────────────────────────
    def _mint_id(self) -> str:
        bid = f"b-{self._seq}"
        self._seq += 1
        return bid

    def add_bullet(self, content: str, section: str, tags: "Optional[List[str]]" = None) -> Bullet:
        """Mint + append a new bullet under *section*. Fail-closed on an empty content/section."""
        content = (content or "").strip()
        section = (section or "").strip()
        if not content:
            raise ValueError("bullet content must be non-empty")
        if not section:
            raise ValueError("bullet section must be non-empty")
        b = Bullet(id=self._mint_id(), content=content, section=section)
        for t in (tags or []):
            b.add_tag(t)
        self.sections.setdefault(section, []).append(b)
        return b

    def get(self, bullet_id: str) -> "Optional[Bullet]":
        for b in self.bullets():
            if b.id == bullet_id:
                return b
        return None

    def remove(self, bullet_id: str) -> bool:
        """Remove a bullet by id (item-level — the primitive selective-unlearning Q-001 builds on). Returns
        True iff a bullet was removed; empties a section list but keeps the (now-empty) section key."""
        for sect, bs in self.sections.items():
            for i, b in enumerate(bs):
                if b.id == bullet_id:
                    del bs[i]
                    return True
        return False

    # ── views ──────────────────────────────────────────────────────────────────────────────────────
    def bullets(self) -> List[Bullet]:
        """All bullets, section order then insertion order (deterministic)."""
        out: List[Bullet] = []
        for sect in self.sections:
            out.extend(self.sections[sect])
        return out

    def section_bullets(self, section: str) -> List[Bullet]:
        return list(self.sections.get(section, []))

    def is_empty(self) -> bool:
        return not any(self.sections.values())

    def __len__(self) -> int:
        return sum(len(bs) for bs in self.sections.values())

    def render(self) -> str:
        """A human-readable AND machine-parsable rendering with explicit boundaries (B-002). Each bullet
        shows its ``[id]``, content, tags and counters so the Generator can cite the bullets it used (H-002).
        The mutable ``(↑helpful ↓harmful)`` counters trail the STABLE ``[id] content #tags`` (C2 #906) so the
        rendered prefix stays cache-friendly across adaptations (KV-cache stable-prefix, N-002). An empty
        playbook renders just the boundary markers (so it injects nothing meaningful when off / unseeded)."""
        lines: List[str] = [_PLAYBOOK_BEGIN]
        for sect in self.sections:
            bs = self.sections[sect]
            if not bs:
                continue
            lines.append(f"## {sect}")
            for b in bs:
                tagstr = (" " + " ".join(f"#{t}" for t in b.tags)) if b.tags else ""
                lines.append(f"- [{b.id}] {b.content}{tagstr} (↑{b.helpful_count} ↓{b.harmful_count})")
        lines.append(_PLAYBOOK_END)
        return "\n".join(lines)

    # ── serialization (M-001) ────────────────────────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "seq": self._seq,
            "sections": {sect: [b.to_dict() for b in bs] for sect, bs in self.sections.items()},
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=False)

    @classmethod
    def from_dict(cls, d: dict) -> "Playbook":
        if not isinstance(d, dict):
            raise ValueError(f"playbook must be an object, got {type(d).__name__}")
        ver = int(d.get("schema_version", SCHEMA_VERSION) or SCHEMA_VERSION)
        if ver > SCHEMA_VERSION:
            raise ValueError(
                f"playbook schema_version {ver} is newer than this build supports ({SCHEMA_VERSION}) — "
                f"refusing fail-closed (cross-version migration is ACE-ROBUST / M-002)")
        raw = d.get("sections", {}) or {}
        if not isinstance(raw, dict):
            raise ValueError("playbook sections must be an object")
        sections: Dict[str, List[Bullet]] = {}
        for sect, bs in raw.items():
            if not isinstance(bs, list):
                raise ValueError(f"section {sect!r} must be a list of bullets")
            sections[sect] = [Bullet.from_dict(b) for b in bs]
        pb = cls(sections=sections, schema_version=SCHEMA_VERSION, _seq=int(d.get("seq", 0) or 0))
        # Defensive: ensure _seq never re-mints an existing id even if the stored counter lagged.
        max_seq = -1
        for b in pb.bullets():
            if b.id.startswith("b-"):
                try:
                    max_seq = max(max_seq, int(b.id[2:]))
                except ValueError:
                    pass
        if pb._seq <= max_seq:
            pb._seq = max_seq + 1
        return pb

    @classmethod
    def from_json(cls, text: str) -> "Playbook":
        try:
            d = json.loads(text)
        except (json.JSONDecodeError, TypeError) as e:
            raise ValueError(f"playbook is not valid JSON: {e}") from e
        return cls.from_dict(d)
