"""``ack.hooks`` — the Loop-Intelligence **Hook-Bus** (epic #602, Teil-2 plan **2.0** — the KEYSTONE).

A standalone, dependency-inverted, fail-soft event bus over the agent loop's boundary points. The
reflection consumers (#602 C2: Verifier 2.1, Quality 2.7, Strategy 2.5, Process-SC 2.2, Lessons 2.3)
**subscribe here** instead of hard-wiring call-sites into ``gx10.py``; the engine **publishes** the
boundary events. This is the substrate the charter calls the keystone "everything else depends on" — it
turns the per-concern ad-hoc dispatch of C1 into one coherent, observable seam.

Design (mirrors the proven :mod:`ack.lessons` fail-soft facade pattern):

* **Dependency inversion** — imports NOTHING from the engine; importing always succeeds.
* **Process-global** ``event -> tuple[Hook]`` registry. :func:`register_hook` is *additive* and
  *idempotent* (a re-registration of the same callable is a no-op, never raises on a duplicate) and is
  fail-**loud** on a typo'd event name or a non-callable — a setup-time programming error caught off the
  hot path (a silently-misspelled event would otherwise never fire).
* **Observer-only** dispatch — a hook's return value is **ignored**. A hook may ABORT by raising (which
  is swallowed); it can never PERMIT/relax a gate (charter design principle 2: gate-adjacent hooks are
  advisory, abort-only).
* **Fail-soft** dispatch — a per-hook exception is swallowed, so one bad subscriber never breaks a turn
  or the sibling hooks. An **O(1) early-out** when no hook is registered makes the default path
  **byte-identical** to the pre-bus engine.
* **Concurrency** (the engine is multi-threaded: server reconciler + agent threads) — copy-on-write
  tuples + snapshot-on-dispatch. :func:`register_hook` / :func:`clear_hooks` take a lock; :func:`dispatch`
  is lock-free (reads an immutable tuple ref), so a registration mid-dispatch never mutates the in-flight
  snapshot. Registration order is preserved → deterministic dispatch order.
* **Cancel / budget awareness without importing the engine** — :func:`dispatch` takes an optional
  ``should_cancel`` predicate and a cumulative ``budget_s`` wall-clock cap, both *injected* by the engine
  (which owns ``_CANCEL_EVENT``). Observer-only, so stopping early is always safe.

**Stability (ADR-0004).** Pre-1.0 the surface is provisional; pin :data:`__version__`. Stdlib only.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional, Tuple

#: Facade contract version (ADR-0004; provisional while < 1.0). Independent of the wheel version.
__version__ = "0.1.0"

# ─── Canonical boundary event set (Teil-2 2.0 + the reconciled charter's ``post_toolresult``) ────────
PRE_TURN = "pre_turn"            #: before prompt assembly for a turn (process-hint / RAG injection site)
POST_GENERATE = "post_generate"  #: a model/agent turn produced an output (the Verifier feed site)
PRE_HANDOVER = "pre_handover"    #: before a task+handover is staged
POST_HANDOVER = "post_handover"  #: after a handover is staged (the Quality-breaker score-feed site)
POST_FEEDBACK = "post_feedback"  #: task completion (the lessons + process-lesson write site)
PRE_ADVANCE = "pre_advance"      #: before the pipeline advances
POST_TOOLRESULT = "post_toolresult"  #: a tool returned (ctx carries the tool name to disambiguate)

#: The one canonical, frozen event set. :func:`register_hook` rejects anything outside it (fail-loud).
HOOK_EVENTS: "Tuple[str, ...]" = (
    PRE_TURN, POST_GENERATE, PRE_HANDOVER, POST_HANDOVER,
    POST_FEEDBACK, PRE_ADVANCE, POST_TOOLRESULT,
)

#: A hook receives the boundary *ctx* (opaque to this module) and returns nothing the bus reads.
Hook = Callable[[Any], Any]

_LOCK = threading.Lock()
_HOOKS: "dict[str, Tuple[Hook, ...]]" = {}


def register_hook(event: str, fn: "Hook") -> None:
    """Subscribe *fn* to *event* (one of :data:`HOOK_EVENTS`). **Additive + idempotent**: a re-registration
    of the same callable object is a no-op (dedup by identity, never double-fires). **Fail-loud** on a
    programming error off the hot path — an unknown *event* raises :class:`ValueError`, a non-callable *fn*
    raises :class:`TypeError`."""
    if event not in HOOK_EVENTS:
        raise ValueError(f"unknown hook event {event!r} (expected one of {HOOK_EVENTS})")
    if not callable(fn):
        raise TypeError("hook must be callable")
    with _LOCK:
        cur = _HOOKS.get(event, ())
        if fn in cur:                  # dedup by identity — additive, never double-fire
            return
        _HOOKS[event] = cur + (fn,)    # copy-on-write: a fresh tuple; in-flight snapshots are untouched


def unregister_hook(event: str, fn: "Hook") -> None:
    """Remove *fn* from *event* (by IDENTITY) — the inverse of :func:`register_hook`. Copy-on-write under the
    lock, so an in-flight :func:`dispatch` keeps its snapshot. A **no-op** when *fn* (or *event*) is not
    registered; never raises. Lets an opt-in consumer cleanly deregister on disable **without clobbering
    sibling hooks** (unlike :func:`clear_hooks`, which drops every hook for the event)."""
    with _LOCK:
        cur = _HOOKS.get(event)
        if not cur:
            return
        rest = tuple(h for h in cur if h is not fn)
        if len(rest) == len(cur):
            return                     # fn was not registered → no-op
        if rest:
            _HOOKS[event] = rest
        else:
            _HOOKS.pop(event, None)    # last hook gone → drop the event (registered_events stays clean)


def dispatch(event: str, ctx: "Any" = None, *,
             should_cancel: "Optional[Callable[[], bool]]" = None,
             budget_s: "Optional[float]" = None) -> None:
    """Fire every hook registered for *event*, in registration order, passing *ctx*. **Observer-only**
    (return values ignored) and **fail-soft** (a per-hook exception is swallowed). **O(1) no-op** when
    nothing is registered → byte-identical. **Lock-free**: snapshots the immutable tuple, so a concurrent
    :func:`register_hook` never perturbs this dispatch. Stops early (safely — observer-only) if
    *should_cancel* returns ``True`` or the cumulative wall-clock exceeds *budget_s*."""
    hooks = _HOOKS.get(event)          # atomic read of an immutable tuple ref (or None)
    if not hooks:                      # O(1) early-out — the byte-identical default path
        return
    start = time.monotonic() if budget_s is not None else 0.0
    for fn in hooks:
        if should_cancel is not None:
            try:
                if should_cancel():
                    return
            except Exception:          # noqa: BLE001 — a broken cancel-check must not break dispatch
                pass
        if budget_s is not None and (time.monotonic() - start) >= budget_s:
            return                     # cumulative budget spent — skip the rest (observer-only: safe)
        try:
            fn(ctx)
        except Exception:              # noqa: BLE001 — one bad subscriber never breaks the turn/others
            pass


def clear_hooks(event: "Optional[str]" = None) -> None:
    """Remove all hooks for *event*, or **all** hooks when *event* is ``None``. For tests / a clean re-init."""
    with _LOCK:
        if event is None:
            _HOOKS.clear()
        else:
            _HOOKS.pop(event, None)


def registered_events() -> "Tuple[str, ...]":
    """The events that currently carry ≥1 hook (sorted) — for introspection / a doctor surface."""
    with _LOCK:
        return tuple(sorted(e for e, hs in _HOOKS.items() if hs))


def hook_count(event: str) -> int:
    """How many hooks are registered for *event* (``0`` if none / unknown)."""
    return len(_HOOKS.get(event, ()))


__all__ = [
    "__version__",
    "HOOK_EVENTS",
    "PRE_TURN", "POST_GENERATE", "PRE_HANDOVER", "POST_HANDOVER",
    "POST_FEEDBACK", "PRE_ADVANCE", "POST_TOOLRESULT",
    "register_hook", "unregister_hook", "dispatch", "clear_hooks", "registered_events", "hook_count",
]
