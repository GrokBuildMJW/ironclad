"""ACE Generator integration — playbook-guided inference + relevant-bullet retrieval + usage tracking.

Epic #855 cluster ACE-GEN (catalogue H-001 playbook-guided inference, H-002 bullet-id tracking, N-004
Generator output format). In ACE the Generator produces the reasoning trajectory; ACE feeds it the playbook
and records which bullets it was given so the Reflector (#858) can rate them.

The load-bearing design choice (resolving the 32k model window without dropping the playbook): the playbook
STORE may be large, but only the **relevant bullet subset** is retrieved into the prompt per query
(fine-grained retrieval, the B-001 itemized-design property). Retrieval is semantic when an embedder is
injected (cosine query↔bullet) and falls back to a lexical token-overlap score otherwise — same INJECTED
embedder contract as ACE-GROW, so this module stays pure / stdlib-only and fail-soft.

This module produces the Generator-side context (`GeneratorContext`) + closes the loop by turning the run's
output into the Reflector's `Trajectory` input (N-004 → N-001).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Optional

from .playbook import Bullet, Playbook
from .grow import cosine, lexical_sim
from .reflector import Trajectory

#: Default number of bullets retrieved into the prompt per query (the relevant subset that fits the window).
DEFAULT_TOP_K = 8

_PLAYBOOK_BEGIN = "=== PLAYBOOK (ACE, relevant subset) BEGIN ==="
_PLAYBOOK_END = "=== PLAYBOOK (ACE) END ==="


@dataclass
class GeneratorContext:
    """What the Generator is given (H-001) + which bullets it was given (H-002, for the Reflector). ``text``
    is the rendered relevant subset to prepend to the prompt; ``bullet_ids`` are the injected ids in rank
    order. An empty playbook yields an empty context (so injecting nothing is byte-identical to no playbook)."""

    text: str = ""
    bullet_ids: List[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.bullet_ids

    def to_dict(self) -> dict:
        return {"text": self.text, "bullet_ids": list(self.bullet_ids)}


def _score(query: str, bullets: "List[Bullet]",
           embed: "Optional[Callable[[List[str]], List[List[float]]]]") -> "List[float]":
    """Relevance of each bullet to *query*: cosine over an injected embedder, else lexical token overlap.
    Fail-soft — an embedder error drops to lexical."""
    if embed is not None:
        try:
            vecs = embed([query] + [b.content for b in bullets])
            if isinstance(vecs, list) and len(vecs) == len(bullets) + 1:
                qv = vecs[0]
                return [cosine(qv, vecs[i + 1]) for i in range(len(bullets))]
        except Exception:  # noqa: BLE001 — fail-soft to lexical
            pass
    return [lexical_sim(query, b.content) for b in bullets]


def select_relevant(playbook: Playbook, query: str, *,
                    embed: "Optional[Callable[[List[str]], List[List[float]]]]" = None,
                    top_k: int = DEFAULT_TOP_K, threshold: float = 0.0) -> "List[Bullet]":
    """The relevant bullet subset for *query*, ranked by relevance (semantic or lexical), highest first.
    Returns at most *top_k* bullets with score >= *threshold*. Ties keep playbook order (deterministic).
    A net-utility tiebreak favors the more-helpful bullet when scores are equal."""
    bullets = playbook.bullets()
    if not bullets or top_k <= 0:
        return []
    scores = _score(query, bullets, embed)
    order = sorted(
        range(len(bullets)),
        key=lambda i: (-scores[i], -bullets[i].net_utility, i),   # score desc, utility desc, stable
    )
    out: List[Bullet] = []
    for i in order:
        if scores[i] < threshold:
            continue
        out.append(bullets[i])
        if len(out) >= top_k:
            break
    return out


def _render_subset(bullets: "List[Bullet]") -> str:
    """Render a selected bullet subset grouped by section, with ids + counters + tags (so the Generator can
    cite ids — H-002). Empty list → empty string (inject nothing)."""
    if not bullets:
        return ""
    by_section: "dict[str, List[Bullet]]" = {}
    for b in bullets:
        by_section.setdefault(b.section, []).append(b)
    lines = [_PLAYBOOK_BEGIN]
    for section, bs in by_section.items():
        lines.append(f"## {section}")
        for b in bs:
            tagstr = (" " + " ".join(f"#{t}" for t in b.tags)) if b.tags else ""
            lines.append(f"- [{b.id}] (↑{b.helpful_count} ↓{b.harmful_count}) {b.content}{tagstr}")
    lines.append(_PLAYBOOK_END)
    return "\n".join(lines)


def prepare_context(playbook: Playbook, query: str, *,
                    embed: "Optional[Callable[[List[str]], List[List[float]]]]" = None,
                    top_k: int = DEFAULT_TOP_K, threshold: float = 0.0) -> GeneratorContext:
    """Build the Generator's playbook context for *query* (H-001): retrieve the relevant subset (fitting the
    window) and render it, tracking the injected bullet ids (H-002). Empty playbook → empty context."""
    selected = select_relevant(playbook, query, embed=embed, top_k=top_k, threshold=threshold)
    return GeneratorContext(text=_render_subset(selected), bullet_ids=[b.id for b in selected])


def to_trajectory(query: str, *, steps: "Optional[List[str]]" = None, outcome: str = "",
                  context: "Optional[GeneratorContext]" = None,
                  used_bullet_ids: "Optional[List[str]]" = None) -> Trajectory:
    """Turn a Generator run into the Reflector's input (N-004 → N-001): the query + reasoning steps + the
    natural execution outcome + the bullet ids the run used (defaulting to the ids that were injected via
    *context*, H-002). This is the closure of the online loop (Generator → Reflector → Curator)."""
    ids = used_bullet_ids if used_bullet_ids is not None else (list(context.bullet_ids) if context else [])
    return Trajectory(query=query, steps=list(steps or []), outcome=outcome, used_bullet_ids=ids)
