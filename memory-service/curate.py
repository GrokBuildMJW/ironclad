"""Pure curated-global helpers for the memory service (#634 / ADR-0011 AD-9, AD-4).

Dependency-free (stdlib only) so the curated-tier rules are unit-testable offline, independent of the live
Mem0/Qdrant stack (app.py connects to Mem0 at import, so its logic cannot be imported offline — these pure
helpers can). ``app.py`` imports these; the live Mem0 instances + the FastAPI wiring stay in app.py.

The curated-global tier is a SEPARATE physical Qdrant collection of operator-promoted, redacted,
cross-project knowledge. It is fed ONLY by the operator-gated ``/promote`` (never the normal ``/add`` path),
and surfaced by an OPT-IN ``/search`` fan-in with PROJECT-WINS precedence — so a project's own memory always
beats a curated entry. Being a SEPARATE collection, it never appears in ``/scopes`` (which scrolls
``agent_memory``) nor in the registry-keyed orphan GC, so curated knowledge can never be flagged an orphan
or reached by the ``/add`` write path.
"""
from __future__ import annotations

from typing import Optional

#: The fixed partition (agent_id) WITHIN the curated-global collection.
CURATED_AGENT_ID = "curated_global"


def promote_refusal(*, confirm: bool, from_agent_id: "Optional[str]",
                    memory: "Optional[str]", query: "Optional[str]") -> "Optional[str]":
    """Fail-closed operator gate for ``/promote``: returns a refusal string, or ``None`` when valid.

    Promotion is NEVER the normal write path: it requires an explicit operator ``confirm`` + a source
    partition (``from_agent_id``) + EXACTLY ONE payload — an operator-redacted ``memory`` text OR a ``query``
    whose source-partition matches are copied by id. An unscoped, unconfirmed, both-or-neither request is
    refused (AD-9 fail-closed)."""
    if not confirm:
        return "promote refused: confirm=true required (operator gate — promotion is never the /add path)"
    if not (isinstance(from_agent_id, str) and from_agent_id.strip()):
        return "promote refused: from_agent_id (the source mem_ns) is required"
    has_mem = bool(isinstance(memory, str) and memory.strip())
    has_q = bool(isinstance(query, str) and query.strip())
    if has_mem and has_q:
        return "promote refused: give EITHER `memory` (redacted text) OR `query` (source matches), not both"
    if not has_mem and not has_q:
        return "promote refused: give `memory` (operator-redacted text) or `query` (source matches to copy)"
    return None


def _mem_text(hit: object) -> str:
    """The memory text of a search hit (Mem0 returns ``{'memory': …}`` or ``{'text': …}``)."""
    if not isinstance(hit, dict):
        return ""
    return str(hit.get("memory") or hit.get("text") or "").strip()


def merge_project_wins(project: dict, curated: dict, limit: int) -> dict:
    """Fan curated-global results in BEHIND the project's own — PROJECT-WINS (#634 / AD-9): the project
    results keep their order + position, then curated entries fill up to *limit*, skipping any whose text a
    project result already carries (a project memory always beats a curated duplicate). Each appended curated
    hit is tagged ``curated: True`` so a reader can tell provenance. Pure over the Mem0 ``{'results': [...]}``
    shape; a missing/odd shape degrades to just the project results."""
    p_results = list((project or {}).get("results") or [])
    c_results = list((curated or {}).get("results") or [])
    try:
        cap = max(0, int(limit))
    except (TypeError, ValueError):
        cap = len(p_results)
    seen = {_mem_text(h) for h in p_results if _mem_text(h)}
    merged = list(p_results)
    for h in c_results:
        if len(merged) >= cap:
            break
        t = _mem_text(h)
        if t and t not in seen:
            merged.append({**h, "curated": True})
            seen.add(t)
    out = dict(project or {})
    out["results"] = merged
    return out
