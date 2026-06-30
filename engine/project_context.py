"""Request-scoped ProjectContext (ADR-0011, AD-1) — the seam that replaces the boot-frozen path/memory
module globals with a per-activation, project-scoped view.

A `contextvars.ContextVar` carries the active project for the current request/turn. Resolution of the
engine's path roots (and, in a later step, the memory partition) consults it, **falling back to the
legacy module globals when it is unset** — so non-project / pre-switch code paths are byte-unchanged
until a project is actually activated (the quiesced switch is a separate step). The active project's
identity comes from the installation Project Registry (`project_registry`, S2).

Background threads do NOT inherit a contextvar set in the spawning thread. Use `bound_target()` (a
`copy_context()` wrapper, captured at SPAWN time in the parent) as the thread target so a spawned daemon /
fan-out worker sees the SAME active ProjectContext as the request that started it — the single most likely
silent isolation regression (ADR-0011 cross-area risk #1). Pure, stdlib-only, secret-free; imports nothing
from the engine.
"""
from __future__ import annotations

import contextvars
import re
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator, Optional

_SAFE_TRACK_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _safe_track(t: object) -> bool:
    """A track id is safe iff non-empty, not ``.``/``..``, and ``[A-Za-z0-9._-]`` only. Mirrors the
    filesystem track guard (gx10._is_safe_track) so the vault subtree and the memory sub-scope resolve
    to the SAME effective track for every input (an unsafe track falls back to ``main`` on both)."""
    return isinstance(t, str) and t not in (".", "..") and bool(_SAFE_TRACK_RE.match(t))


@dataclass(frozen=True)
class ProjectContext:
    """The active project for the current request/turn. Generic isolation identity (a snapshot of the
    registry descriptor's isolation fields); the DEV target overlay is resolved separately."""
    project_id: str
    root: str                                  # absolute project root
    mem_ns: str                                # memory partition key
    track: str = "main"                        # active track within the project

    def mem_scope(self) -> str:
        """The track-composed memory partition (ADR-0011 AD-2'/AD-4 / S14-1): ``<mem_ns>::track::<tid>``
        for a non-``main`` (and safe) track, else the bare ``mem_ns``. An empty ``mem_ns`` (the base
        partition) is returned unchanged — the legacy base is never track-suffixed, so a single-project /
        ``main``-track install is byte-identical. An unsafe track falls back to ``main`` (no suffix),
        matching the vault subtree resolution."""
        t = self.track if _safe_track(self.track) else "main"
        if self.mem_ns and t != "main":
            return f"{self.mem_ns}::track::{t}"
        return self.mem_ns


_CTX: "contextvars.ContextVar[Optional[ProjectContext]]" = contextvars.ContextVar(
    "ironclad_project_context", default=None)


def current() -> Optional[ProjectContext]:
    """The active ProjectContext for this thread/turn, or None (→ callers fall back to legacy globals)."""
    return _CTX.get()


def set_current(ctx: Optional[ProjectContext]) -> "contextvars.Token":
    """Set the active context, returning a token for `reset`. Prefer `use()` for scoped activation."""
    return _CTX.set(ctx)


def reset(token: "contextvars.Token") -> None:
    _CTX.reset(token)


@contextmanager
def use(ctx: Optional[ProjectContext]) -> Iterator[Optional[ProjectContext]]:
    """Scoped activation: `with use(ctx): ...` sets the active context for the block and restores the
    previous one on exit (even on exception)."""
    token = _CTX.set(ctx)
    try:
        yield ctx
    finally:
        _CTX.reset(token)


def bound_target(fn: Callable, *args, **kwargs) -> Callable[[], object]:
    """Return a zero-arg callable that runs `fn(*args, **kwargs)` bound to the context CURRENT AT THIS CALL
    (the parent/spawning thread). This is the thread-binding primitive — pass it as the thread target so the
    child observes the active ProjectContext:

        threading.Thread(target=bound_target(work, a, b)).start()

    The `copy_context()` is taken HERE (at spawn time). Do NOT defer it into the child
    (`target=lambda: copy_context().run(...)` would copy the child's empty context and see `current() is
    None` — a cross-project leak once the switch is live)."""
    ctx = contextvars.copy_context()
    return lambda: ctx.run(fn, *args, **kwargs)
