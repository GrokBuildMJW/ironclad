"""Pure scope-isolation guards for the memory service (ADR-0011 AD-4 / Ironclad #601 S15).

Kept dependency-free (stdlib only — NO Mem0 / FastAPI / Qdrant imports) so the isolation rules are
unit-testable offline, independently of the live memory stack. ``app.py`` imports and enforces them.

AD-4 partitioning rule: every memory write/search MUST be scoped to a project/track partition via
``agent_id`` (the ``mem_ns``); ``run_id`` is **not** an isolation key in this deployment (the partition is
``agent_id`` only — run identifiers belong in metadata, never as a hidden isolation dimension).
"""
from __future__ import annotations

from typing import Optional


def require_scope(agent_id: "Optional[str]", run_id: "Optional[str]" = None) -> "Optional[str]":
    """Validate that a write/search is properly scoped. Returns an **error string** to refuse the request
    (the caller raises HTTP 400), or ``None`` when it is well-scoped.

    - ``agent_id`` (the ``mem_ns`` partition) is **required** and must be a non-blank string — an unscoped
      write/search would leak across the shared store, so it is refused.
    - ``run_id`` must **not** be used as an isolation key: a request that sets it is refused (AD-4 — the
      partition is ``agent_id`` only; pass run identifiers in ``metadata`` instead).
    """
    if not (isinstance(agent_id, str) and agent_id.strip()):
        return "agent_id (mem_ns) is required: every memory operation must be scoped to a partition"
    if run_id is not None and str(run_id).strip():
        return ("run_id is not an isolation key — partition by agent_id (mem_ns); "
                "pass run identifiers in metadata")
    return None
