"""Loop Profiles — per-TaskType loop limits (Agent-Contract-Kernel, #602 S602-8a).

> **One loop budget does not fit every task.** A `research` task may want more iterations than a
> `documentation` one; a `chat` task may want a tighter retry budget. (Per-type `retry_budget` can only
> be *lowered* — it is clamped to the hard re-ask ceiling; see below.) This module is the **pure
> resolver** that deep-merges a per-`TaskType` :class:`LoopProfile` over the engine's code defaults.

:func:`resolve_loop_profile` is **pure** (no transport, no model, no I/O) and **never raises** — so it is
snapshot-testable and safe to call on the hot path. The SCHEMA + DEFAULTS live in the engine's config tree
(``loop_profiles`` in ``_code_defaults``); the *private* monorepo may ship an override layer, but ``core/``
carries only generic defaults. The engine passes its own ``MAX_ITERATIONS`` / retry-budget constants as the
fallbacks, so with NO ``loop_profiles`` configured the resolved profile is **byte-identical to today's
behaviour** (the global limits), and the public clean-room export is unchanged.

Precedence (low → high): code defaults ← ``loop_profiles['default']`` ← ``loop_profiles['by_type'][type]``.
Only PRESENT (non-``None``) keys override, so a partial override layers cleanly. ``retry_budget`` is clamped
to ``[1, max_retry_budget]`` (the hard re-ask ceiling stays authoritative). Imports only the stdlib.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class LoopProfile:
    """The resolved per-task loop budget: how many agent iterations, the re-ask budget, the effort tier, and
    (#602 S602-8b) which evaluation verifiers a consumer should run for this task type.

    ``eval_verifiers`` is an ADVISORY activation list (the verifiers are MARK-ONLY — :mod:`ack.verify`; they
    never gate); empty by default ⇒ byte-identical (no evaluation runs)."""

    max_iterations: int
    retry_budget: int
    effort: str
    eval_verifiers: "tuple[str, ...]" = ()


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:   # noqa: BLE001 — coercing untrusted config; the resolver never raises
        return default


def _type_key(task_type: Any) -> Optional[str]:
    """The ``by_type`` lookup key for *task_type* — a ``TaskType``/str-enum's ``.value`` or the str itself;
    ``None`` (no task type) selects no per-type layer."""
    if task_type is None:
        return None
    val = getattr(task_type, "value", task_type)
    try:
        return str(val)
    except Exception:   # noqa: BLE001
        return None


def resolve_loop_profile(
    profiles: Any,
    task_type: Any,
    *,
    default_max_iterations: int,
    default_retry_budget: int,
    max_retry_budget: int,
    default_effort: str = "medium",
) -> LoopProfile:
    """Deep-merge a per-``TaskType`` :class:`LoopProfile` over the engine's code defaults. Pure; **never
    raises** (any malformed input falls back to the code defaults).

    *profiles* is the ``loop_profiles`` config block (``{"default": {...}, "by_type": {"<type>": {...}}}``);
    *task_type* is the active task's type (a ``TaskType``, a str, or ``None`` for the chat loop). The
    ``default_*`` fallbacks are the engine's current globals — so an absent/empty ``loop_profiles`` yields a
    profile byte-identical to today's limits.
    """
    base_mi, base_rb, base_eff = default_max_iterations, default_retry_budget, default_effort
    try:
        mi, rb, eff, ev = base_mi, base_rb, base_eff, ()
        mi_set = rb_set = False
        p = profiles if isinstance(profiles, dict) else {}
        by_type = p.get("by_type") if isinstance(p.get("by_type"), dict) else {}
        key = _type_key(task_type)
        for layer in (p.get("default"), by_type.get(key) if key is not None else None):
            if isinstance(layer, dict):
                if layer.get("max_iterations") is not None:
                    mi, mi_set = layer["max_iterations"], True
                if layer.get("retry_budget") is not None:
                    rb, rb_set = layer["retry_budget"], True
                if layer.get("effort") is not None:
                    eff = layer["effort"]
                if layer.get("eval") is not None:   # #602 8b: per-profile eval activation (replace, like the rest)
                    ev = layer["eval"]
        # Floor/clamp ONLY operator-supplied overrides; the engine fallback passes through VERBATIM so an
        # existing deployment (e.g. context.max_iterations=0 → today's zero iterations) stays byte-identical.
        if mi_set:
            mi = max(1, _as_int(mi, base_mi))
        if rb_set:
            rb = max(1, min(_as_int(rb, base_rb), max(1, _as_int(max_retry_budget, base_rb))))
        if not isinstance(eff, str) or not eff:
            eff = base_eff
        eval_verifiers = tuple(x for x in ev if isinstance(x, str)) if isinstance(ev, (list, tuple)) else ()
        return LoopProfile(mi, rb, eff, eval_verifiers)
    except Exception:   # noqa: BLE001 — pathological config → the engine fallbacks verbatim (never raises)
        return LoopProfile(base_mi, base_rb, base_eff)
