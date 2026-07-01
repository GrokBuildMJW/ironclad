"""ACE Reflector — distils concrete insights from a reasoning trajectory + execution feedback.

Epic #855 cluster ACE-REFLECT (catalogue E-001 trajectory analysis, E-002 iterative refinement, E-003
label-free robustness, E-004 bullet rating, N-001 input format, N-002 output format). In ACE the Reflector
is the role that critiques the Generator's trace — separating *evaluation* from *curation* — and proposes
(a) new candidate bullets and (b) helpful/harmful ratings of the bullets that were used. The Curator (#859)
turns these into deterministic playbook deltas.

Transport-injected + pure-otherwise (stdlib only): the LLM call is an injected ``chat`` callable
(``Callable[[str], str]``), mirroring how ``ack.verify.verify_with_judge`` injects its ``chat`` — so this
module imports nothing from the engine and is unit-tested with a fake. The async background worker + budget
gate + the real orchestrator-model transport are wired in ACE-ADAPT-ONLINE (#862) / ACE-WIRE (#863).

FAIL-SOFT: ``reflect`` never raises — a transport error or unparseable model output yields an EMPTY
``ReflectorOutput`` (reflection is advisory; a hiccup must never break the loop), exactly like the #602 seams.
LABEL-FREE (E-003): the signal is ``Trajectory.outcome`` (natural execution feedback), never a ground-truth
label.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from .playbook import Bullet, DEFAULT_SECTIONS, HELPFUL, HARMFUL, NEUTRAL

#: The verdicts the Reflector may assign to a USED bullet (E-004). Anything else is dropped.
_VERDICTS = (HELPFUL, HARMFUL, NEUTRAL)


@dataclass
class Trajectory:
    """The Reflector input (N-001): the query, the reasoning/tool steps, the natural execution ``outcome``
    (success / failure / free-text feedback — NOT a label, E-003), and the ids of the playbook bullets the
    Generator used on this run (from H-002, #861) so the Reflector can rate them."""

    query: str
    steps: List[str] = field(default_factory=list)
    outcome: str = ""
    used_bullet_ids: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"query": self.query, "steps": list(self.steps),
                "outcome": self.outcome, "used_bullet_ids": list(self.used_bullet_ids)}

    @classmethod
    def from_dict(cls, d: dict) -> "Trajectory":
        if not isinstance(d, dict):
            raise ValueError("trajectory must be an object")
        return cls(query=str(d.get("query", "")),
                   steps=[str(s) for s in (d.get("steps") or [])],
                   outcome=str(d.get("outcome", "")),
                   used_bullet_ids=[str(b) for b in (d.get("used_bullet_ids") or [])])


@dataclass
class CandidateBullet:
    """An extracted insight (N-002): a reusable strategy / domain concept / failure mode the Curator may
    add to the playbook. Carries no id (the Playbook mints it on add)."""

    content: str
    section: str
    tags: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"content": self.content, "section": self.section, "tags": list(self.tags)}


@dataclass
class BulletRating:
    """A helpful/harmful/neutral verdict on a USED bullet (E-004) — feeds the playbook counters."""

    bullet_id: str
    verdict: str

    def to_dict(self) -> dict:
        return {"bullet_id": self.bullet_id, "verdict": self.verdict}


@dataclass
class ReflectorOutput:
    """The Reflector result (N-002): candidate bullets + ratings of the used bullets, plus how many
    refinement rounds actually ran. Empty (no insights, no ratings) is the fail-soft / nothing-to-learn case."""

    insights: List[CandidateBullet] = field(default_factory=list)
    ratings: List[BulletRating] = field(default_factory=list)
    rounds_run: int = 0

    def is_empty(self) -> bool:
        return not self.insights and not self.ratings

    def to_dict(self) -> dict:
        return {"insights": [i.to_dict() for i in self.insights],
                "ratings": [r.to_dict() for r in self.ratings], "rounds_run": self.rounds_run}


def _extract_json_object(text: str) -> Optional[dict]:
    """Best-effort: parse *text* as a JSON object, else grab the first balanced ``{...}`` block. Returns
    None on failure (the model emitted prose / nothing parseable) — never raises."""
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except (json.JSONDecodeError, ValueError):
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except (json.JSONDecodeError, ValueError):
        return None


def _parse(obj: dict, *, sections) -> "tuple[List[CandidateBullet], List[BulletRating]]":
    """Validate + coerce a parsed Reflector response into typed insights + ratings; drop malformed entries
    (robustness: a noisy model output yields fewer items, never an exception)."""
    insights: List[CandidateBullet] = []
    for it in (obj.get("insights") or []):
        if not isinstance(it, dict):
            continue
        content = str(it.get("content", "")).strip()
        section = str(it.get("section", "")).strip()
        if not content:
            continue
        if section not in sections:          # snap an unknown/blank section to the first canonical one
            section = sections[0]
        tags = [str(t).strip() for t in (it.get("tags") or []) if str(t).strip()]
        insights.append(CandidateBullet(content=content, section=section, tags=tags))
    ratings: List[BulletRating] = []
    seen: set = set()
    for rt in (obj.get("ratings") or []):
        if not isinstance(rt, dict):
            continue
        bid = str(rt.get("bullet_id", "")).strip()
        verdict = str(rt.get("verdict", "")).strip().lower()
        if not bid or verdict not in _VERDICTS or bid in seen:
            continue
        seen.add(bid)
        ratings.append(BulletRating(bullet_id=bid, verdict=verdict))
    return insights, ratings


def _analysis_prompt(traj: Trajectory, used: "List[Bullet]", sections) -> str:
    used_block = "\n".join(f"  [{b.id}] {b.content}" for b in used) or "  (none)"
    return (
        "You are the Reflector in an Agentic Context Engineering loop. Analyze the trajectory below and "
        "distil REUSABLE, concrete insights — strategies, domain concepts, or recurring failure modes — that "
        "would help on similar future tasks. Use the natural execution outcome as your signal (no labels).\n\n"
        f"QUERY:\n{traj.query}\n\n"
        f"REASONING STEPS:\n" + ("\n".join(f"  - {s}" for s in traj.steps) or "  (none)") + "\n\n"
        f"OUTCOME (execution feedback):\n{traj.outcome or '(none)'}\n\n"
        f"PLAYBOOK BULLETS USED THIS RUN:\n{used_block}\n\n"
        f"Allowed sections: {', '.join(sections)}.\n"
        "Respond with STRICT JSON only (no prose), shape:\n"
        '{"insights":[{"content":"...","section":"<one of the allowed>","tags":["helpful|harmful|neutral|..."]}],'
        '"ratings":[{"bullet_id":"<id from the list above>","verdict":"helpful|harmful|neutral"}]}\n'
        "Only rate bullets that were actually used; only add insights that are genuinely new + actionable. "
        "If there is nothing to learn, return empty arrays."
    )


def _refine_prompt(prev: "List[CandidateBullet]", sections) -> str:
    draft = json.dumps([i.to_dict() for i in prev], ensure_ascii=False)
    return (
        "Refine the following draft insights from the previous round: remove duplicates and anything not "
        "genuinely reusable, sharpen the wording, and keep each as a single atomic bullet. Keep the same JSON "
        f"shape (insights only). Allowed sections: {', '.join(sections)}.\nDRAFT:\n{draft}\n"
        'Respond with STRICT JSON only: {"insights":[{"content":"...","section":"...","tags":[...]}]}'
    )


def reflect(trajectory: Trajectory, *, chat: Callable[[str], str],
            used_bullets: "Optional[List[Bullet]]" = None, rounds: int = 1,
            sections=DEFAULT_SECTIONS) -> ReflectorOutput:
    """Run the Reflector over *trajectory* using the injected *chat* transport. Produces candidate bullets +
    ratings of the *used_bullets*. ``rounds`` (>=1, default 1 per the C0 product default) controls iterative
    refinement of the insights (E-002); the paper's larger counts are an offline-research setting. FAIL-SOFT:
    any transport/parse error → an empty result; never raises."""
    used = list(used_bullets or [])
    sections = tuple(sections) or DEFAULT_SECTIONS
    rounds = max(1, int(rounds or 1))
    try:
        first = chat(_analysis_prompt(trajectory, used, sections))
    except Exception:  # noqa: BLE001 — reflection is advisory; a transport hiccup must not break the loop
        return ReflectorOutput(rounds_run=0)
    obj = _extract_json_object(first)
    if obj is None:
        return ReflectorOutput(rounds_run=1)        # called the model but got nothing parseable
    insights, ratings = _parse(obj, sections=sections)
    rounds_run = 1
    for _ in range(rounds - 1):
        if not insights:
            break
        try:
            resp = chat(_refine_prompt(insights, sections))
        except Exception:  # noqa: BLE001 — keep the prior round's insights on a refine error
            break
        rounds_run += 1
        ro = _extract_json_object(resp)
        if ro is None:
            break
        refined, _ = _parse(ro, sections=sections)
        if refined:
            insights = refined                       # a successful refine replaces the draft
    return ReflectorOutput(insights=insights, ratings=ratings, rounds_run=rounds_run)
