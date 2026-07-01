"""ACE grow-and-refine — keep the evolving playbook compact + relevant via de-duplication and pruning.

Epic #855 cluster ACE-GROW (catalogue D-001 growth with de-duplication, D-002 lazy vs proactive refinement,
D-003 pruning on overflow, L-003 de-dup threshold, L-004 pruning trigger). ACE bullets grow by appending
(the Curator's ADD ops) and update in place (counters); grow-and-refine then prunes redundancy and bounds the
size so the playbook stays interpretable and never collapses.

De-duplication is **semantic** when an embedder is available (cosine over bullet-content vectors at a
configurable threshold, L-003) and falls back to a **lexical** floor (token-set Jaccard) when no embedder is
reachable (sealed / no-memory deployments) — the C0 decision. The embedder is INJECTED
(``Callable[[List[str]], List[List[float]]]``, batched), so this module stays pure / stdlib-only and is
unit-tested with a fake. Pruning is **utility-based** (lowest helpful−harmful first, then oldest) when the
playbook exceeds a bullet-count or rendered-size budget (L-004; the real model-window coordination wires in
#862/#863). Refinement runs **proactively** (after each delta) or **lazily** (only over budget) per D-002.

FAIL-SOFT: an embedder error inside ``dedupe`` falls back to lexical; nothing here raises on a hiccup.
"""
from __future__ import annotations

import math
import re
from typing import Callable, List, Optional

from .playbook import Bullet, Playbook

_WORD = re.compile(r"[a-z0-9]+")

#: Default semantic de-dup threshold (cosine). Stable in the paper's 0.7–0.9 band; 0.9 = "near-identical".
DEFAULT_DEDUP_THRESHOLD = 0.9


def cosine(a: "List[float]", b: "List[float]") -> float:
    """Cosine similarity of two vectors; 0.0 on a zero/empty/mismatched vector (never raises)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _tokens(text: str) -> set:
    return set(_WORD.findall((text or "").lower()))


def lexical_sim(s1: str, s2: str) -> float:
    """Token-set Jaccard similarity (the no-embedder fallback floor). 1.0 for identical token sets."""
    t1, t2 = _tokens(s1), _tokens(s2)
    if not t1 and not t2:
        return 1.0
    if not t1 or not t2:
        return 0.0
    inter = len(t1 & t2)
    return inter / len(t1 | t2)


def _merge_into(keep: Bullet, drop: Bullet) -> None:
    """Fold *drop* into *keep* (grow-and-refine: existing bullet updated in place): sum the usage counters,
    union the tags. *keep*'s content + id survive (it is the earlier / higher-utility bullet)."""
    keep.helpful_count += drop.helpful_count
    keep.harmful_count += drop.harmful_count
    for t in drop.tags:
        keep.add_tag(t)


def dedupe(playbook: Playbook, *, embed: "Optional[Callable[[List[str]], List[List[float]]]]" = None,
           threshold: float = DEFAULT_DEDUP_THRESHOLD) -> int:
    """Merge near-duplicate bullets **within each section** (cross-section bullets are never duplicates).
    Semantic (cosine ≥ *threshold*) when *embed* is given + succeeds, else the lexical Jaccard floor. The
    earlier bullet in insertion order is kept (deterministic) and the later folded into it. Returns the number
    of bullets merged away."""
    merged = 0
    for section, bullets in list(playbook.sections.items()):
        if len(bullets) < 2:
            continue
        vectors: "Optional[List[List[float]]]" = None
        if embed is not None:
            try:
                vectors = embed([b.content for b in bullets])
                if not (isinstance(vectors, list) and len(vectors) == len(bullets)):
                    vectors = None
            except Exception:  # noqa: BLE001 — fail-soft: a bad embedder drops to the lexical floor
                vectors = None
        survivors: List[Bullet] = []
        survivor_idx: List[int] = []
        for i, b in enumerate(bullets):
            dup_of = -1
            for sj, j in zip(survivors, survivor_idx):
                sim = cosine(vectors[i], vectors[j]) if vectors is not None else lexical_sim(b.content, sj.content)
                if sim >= threshold:
                    dup_of = survivors.index(sj)
                    break
            if dup_of >= 0:
                _merge_into(survivors[dup_of], b)
                merged += 1
            else:
                survivors.append(b)
                survivor_idx.append(i)
        playbook.sections[section] = survivors
    return merged


def _rendered_chars(playbook: Playbook) -> int:
    return len(playbook.render())


def prune(playbook: Playbook, *, max_bullets: "Optional[int]" = None,
          max_chars: "Optional[int]" = None) -> int:
    """Bound the playbook (L-004 trigger): while it exceeds *max_bullets* (count) or *max_chars* (rendered
    size — a proxy for the model-window budget), remove the LEAST useful bullet first — lowest net_utility,
    ties broken by higher harmful_count then oldest id (D-003). Returns the number pruned. A None budget is
    not enforced."""
    pruned = 0

    def _over() -> bool:
        if max_bullets is not None and len(playbook) > max_bullets:
            return True
        if max_chars is not None and _rendered_chars(playbook) > max_chars:
            return True
        return False

    def _seq(b: Bullet) -> int:
        try:
            return int(b.id[2:]) if b.id.startswith("b-") else 0
        except ValueError:
            return 0

    while _over():
        candidates = playbook.bullets()
        if not candidates:
            break
        # least useful first: lowest net_utility, then most harmful, then oldest (smallest seq). NB
        # net_utility + harmful_count are ordered for min(), but seq is POSITIVE so min() picks the SMALLEST
        # seq = the OLDEST bullet (D-003); `-_seq` would wrongly evict the NEWEST (C2 #902).
        victim = min(candidates, key=lambda b: (b.net_utility, -b.harmful_count, _seq(b)))
        if not playbook.remove(victim.id):
            break
        pruned += 1
    return pruned


def refine(playbook: Playbook, *, embed: "Optional[Callable[[List[str]], List[List[float]]]]" = None,
           dedup_threshold: float = DEFAULT_DEDUP_THRESHOLD, max_bullets: "Optional[int]" = None,
           max_chars: "Optional[int]" = None, lazy: bool = False) -> dict:
    """Grow-and-refine in one call: de-duplicate, then prune to budget. ``lazy`` (D-002): when True and the
    playbook is already within budget, do NOTHING (refine only on overflow); when False (proactive), always
    de-dup + prune. Returns ``{merged, pruned, ran}``."""
    over_budget = (
        (max_bullets is not None and len(playbook) > max_bullets)
        or (max_chars is not None and _rendered_chars(playbook) > max_chars)
    )
    if lazy and not over_budget:
        return {"merged": 0, "pruned": 0, "ran": False}
    merged = dedupe(playbook, embed=embed, threshold=dedup_threshold)
    pruned = prune(playbook, max_bullets=max_bullets, max_chars=max_chars)
    return {"merged": merged, "pruned": pruned, "ran": True}
