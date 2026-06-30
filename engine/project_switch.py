"""Quiesced project switch (ADR-0011 AD-1 / S5) — atomically rebind the engine to another project.

Dependency-injected: imports nothing from ``gx10`` (no cycle). The S5b wiring passes the live *agent*, the
*registry*, the *base_cfg*, and the *apply_config* callable; this module is the pure orchestration of the
quiesced switch, in the order that makes it leak-free.

Operator decisions (#624):
- **Refuse** the switch if a dev unit is in-flight for the leaving OR entering project (``in_flight``).
- **Per-project conversation**: save the LEAVING project's session BEFORE rebinding (so it lands under the
  leaving root), then load the ENTERING project's session AFTER rebinding (or start fresh from its system
  prompt) — the live ``agent.messages`` is REPLACED, never appended (no A→B in-memory bleed, ADR-0011 #2).
- **Config**: rebuild the per-project effective config via ``apply_project_overlay`` (locked keys dropped).
  Rebuilt on EVERY call — including a same-project re-assert — so a changed overlay or a boot-repair takes
  effect; only the session save/load churn is skipped when the project does not actually change.
- The warm rolling summary re-keys automatically via the active ProjectContext (``_active_warm_session``)
  and is **never deleted**.

Ordering / crash-safety:
- The leaving session is saved with the ctx bound to the LEAVING project (set here, defensively, so the
  save lands under the leaving root regardless of the caller's current ctx).
- After the entering ctx is bound, any failure performs a FULL rollback to the leaving project: the ctx is
  restored, and (for a real switch) the leaving project's effective config is re-applied and its just-saved
  conversation reloaded — so a raised switch never leaves a half-switched engine (ctx/config/messages all
  back on the leaving project). The rollback restore is best-effort (a nested failure does not mask the
  original error). ``set_active`` is committed LAST (inside the rollback guard), so the registry + the
  caller's active cache are never moved off the leaving project on a failed switch.

MUST be called while holding the engine's single agent lock (single-active-per-engine, AD-1), so the
save→rebind→load sequence is atomic w.r.t. turns.
"""
from __future__ import annotations

from typing import Callable, Optional, Tuple

import project_context as _pc
import project_overlay as _po


class SwitchRefused(RuntimeError):
    """The switch was refused because a dev unit is in-flight for the leaving or entering project."""


def _ctx_of(project) -> "_pc.ProjectContext":
    """The ProjectContext for *project* (track-aware; defaults to ``main``)."""
    return _pc.ProjectContext(project.id, project.root, project.mem_ns,
                              getattr(project, "active_track", "main") or "main")


def switch_project(target_id: str, *,
                   registry,
                   agent,
                   base_cfg: dict,
                   apply_config: Callable[[dict], None],
                   overlay_for: "Callable[[object], dict]" = lambda p: {},
                   in_flight: "Callable[[str], bool]" = lambda pid: False,
                   ctx_for: "Callable[[object], _pc.ProjectContext]" = _ctx_of) -> "Tuple[object, list]":
    """Rebind the engine's active project to *target_id*. Returns ``(target_project, dropped_overlay_keys)``.
    Raises ``KeyError`` if *target_id* is unknown, ``SwitchRefused`` if a dev unit is in-flight.

    Injected seams (so this never imports the engine):
      - ``registry``: ``.get(id)→Project|None``, ``.active()→Project|None``, ``.set_active(id)``.
      - ``agent``:
          * ``.save_session()`` — persist the current conversation under the active ctx's root.
          * ``.load_session()→bool`` — REPLACE ``agent.messages`` entirely (INCLUDING the system message)
            with the target's persisted conversation; return True iff a session existed. The leaving
            project's system prompt must NOT survive the load (the S5b GX10 adapter resets the system
            prompt to the target's and coerces GX10's int return to bool).
          * ``.start_fresh(prompt_path)`` — set ``agent.messages`` to exactly the target's system message.
      - ``base_cfg``: the deployment base config (the switch never mutates it).
      - ``apply_config(merged)``: re-derive the engine globals from the merged config.
      - ``overlay_for(project)→dict``: the project's config overlay (default empty).
      - ``in_flight(project_id)→bool``: True iff a dev unit is currently running for that project.
      - ``ctx_for(project)→ProjectContext``: build the ProjectContext to bind for *project*. The default
        maps id/root/mem_ns/track 1:1; the engine injects a policy variant (e.g. the implicit ``default``
        project binds an EMPTY mem_ns so it shares the base/legacy memory partition — backward-compatible).
    """
    target = registry.get(target_id)
    if target is None:
        raise KeyError(f"unknown project: {target_id}")

    leaving = registry.active()

    # Refuse if either side has a dev unit running (fail-closed — never switch out/into busy work).
    for pid in {p.id for p in (leaving, target) if p is not None}:
        if in_flight(pid):
            raise SwitchRefused(f"a dev unit is in-flight for project {pid!r}; switch refused")

    same = leaving is not None and leaving.id == target.id

    # 1. persist the LEAVING conversation under ITS root — bind the leaving ctx first (defensive: the
    #    caller's ctx may be stale/unset), then save. Skipped on a same-project re-assert (no churn).
    if not same and leaving is not None:
        _pc.set_current(ctx_for(leaving))
        agent.save_session()

    # Restore point: ctx + (uncommitted) registry currently both agree on `leaving`.
    prior = ctx_for(leaving) if leaving is not None else None
    try:
        # 2. bind the entering project's context (path + memory resolution now point at the target).
        _pc.set_current(ctx_for(target))

        # 3. rebuild the per-project effective config (locked keys dropped). ALWAYS — also on a
        #    same-project re-assert, so a changed overlay / boot-repair takes effect.
        merged, dropped = _po.apply_project_overlay(base_cfg, overlay_for(target) or {})
        apply_config(merged)

        # 4. REPLACE the conversation only when the project actually changes (never discard live turns).
        if not same:
            if not agent.load_session():
                prompt_path = (merged.get("paths") or {}).get("system_prompt") or ""
                agent.start_fresh(prompt_path)
            # 5. commit the switch LAST — but INSIDE the rollback guard, so a failed commit (registry
            #    write error) rolls the ctx back to the leaving project instead of leaving the registry
            #    on `leaving` while the ctx is already on `target` (a half-switched engine).
            registry.set_active(target.id)
    except Exception:
        # FULL rollback to the leaving project, so a raised switch never leaves a half-switched engine.
        # ctx first; then — for a REAL switch (the leaving conversation was saved + the config/conversation
        # may already be the target's) — restore the leaving project's effective config and reload its
        # just-saved conversation. Best-effort (a nested failure must not mask the original); the registry
        # + the caller's active cache were never moved off `leaving`.
        _pc.set_current(prior)
        if not same and leaving is not None:
            try:
                lmerged, _ = _po.apply_project_overlay(base_cfg, overlay_for(leaving) or {})
                apply_config(lmerged)
                agent.load_session()
            except Exception:
                pass
        raise

    return target, dropped
