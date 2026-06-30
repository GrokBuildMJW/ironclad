"""``ack.lessons`` — the curated, versioned LessonStore / LessonProvider seam (ADR-0011 AD-10).

The stable delegation surface for **scope-partitioned actionable lessons** — the "loop-intelligence"
tier layered on the memory substrate. This rework (#601) owns the SUBSTRATE + this API; the lesson
*semantics* (distillation, ranking, the persistent backend) are supplied by a registered provider —
**epic #602's Distiller goes through this API ONLY**, never touching `mem_ns` internals or the Valkey/
Mem0 keys.

**Dependency inversion.** This module imports NOTHING from the engine: it exposes a tiny
:class:`LessonProvider` protocol and a process-global registration seam (:func:`set_provider`). The
engine (or #602) registers a concrete provider; with **no provider registered the API is a fail-soft
no-op** — reads return ``[]``/``""`` and writes do nothing, so a lesson backend's absence (the wheel
installed on its own, no engine, no #602) can never break a turn. Importing this facade always succeeds.

**Fail-soft reads/writes vs fail-closed promotion.** ``get_lessons``/``report_lesson``/``brief`` run on
the hot path and are advisory, so they **swallow** a provider error (lessons are a hint, never a
dependency). :func:`promote`, by contrast, is **fail-closed** (AD-9): a project-private lesson — which may
carry paths/secrets — is promoted to a broader scope ONLY through a redactor that approves the redacted
text; a missing/refusing redactor raises.

**Scope.** The ``scope`` is an opaque partition string (the engine passes its active ``mem_scope`` —
``<mem_ns>::track::<tid>``). This module never parses or mints it; it is the provider's partition key.
Lessons are **project-private by default** (the scope IS the project/track partition); cross-scope flow
happens only via :func:`promote`.

**Stability (ADR-0004).** Pre-1.0 the surface is provisional; from 1.0 it follows semver. Pin
:data:`__version__`. Zero external dependencies (stdlib only).
"""
from __future__ import annotations

from typing import Any, Callable, List, Optional, Protocol, Sequence, runtime_checkable

#: Facade contract version (ADR-0004; provisional while < 1.0). Independent of the wheel version.
__version__ = "0.1.0"


@runtime_checkable
class LessonProvider(Protocol):
    """The seam a concrete lesson backend (e.g. epic #602's Distiller) registers via
    :func:`set_provider`. Signatures are the stable contract; the facade only delegates. A provider must
    treat *scope* as an opaque partition key and keep scopes isolated (project-private by default)."""

    def get_lessons(self, scope: str, query: str = "", limit: int = 10) -> "List[str]":
        """Return up to *limit* lessons for *scope* (optionally ranked by *query*); ``[]`` if none."""
        ...

    def report_lesson(self, scope: str, lesson: str, metadata: "Optional[dict]" = None) -> None:
        """Record *lesson* under *scope* (fire-and-forget from the engine's perspective)."""
        ...

    def brief(self, scopes: "Sequence[str]", limit: int = 10) -> str:
        """A scope-PRIORITY merged digest across *scopes* (earlier scopes win); ``""`` if none."""
        ...


_PROVIDER: "Optional[LessonProvider]" = None


def set_provider(provider: "Optional[LessonProvider]") -> None:
    """Register (or, with ``None``, clear) the process-global lesson provider. The engine / #602 calls
    this once; tests pass a fake. Idempotent; last registration wins."""
    global _PROVIDER
    _PROVIDER = provider


def get_provider() -> "Optional[LessonProvider]":
    """The currently registered provider, or ``None`` when no lesson backend is wired (fail-soft no-op)."""
    return _PROVIDER


# ─── Hot-path verbs — fail-soft (advisory; a provider error never breaks a turn) ──────────────────
def get_lessons(scope: str, query: str = "", limit: int = 10) -> "List[str]":
    """Lessons for *scope* (≤ *limit*), or ``[]`` when no provider is wired / on any provider error."""
    p = _PROVIDER
    if p is None:
        return []
    try:
        out = p.get_lessons(scope, query=query, limit=limit)
    except Exception:   # noqa: BLE001 — advisory: a lesson-read failure must never break a turn
        return []
    # Reject provider garbage: a lesson list must be a list/tuple of STRINGS. A scalar str/bytes/dict
    # would otherwise iterate into chars/ints/keys and leak (and a non-str item would later break brief's
    # dedup/join). Filter to str items, never raising.
    if not isinstance(out, (list, tuple)):
        return []
    return [x for x in out if isinstance(x, str)]


def report_lesson(scope: str, lesson: str, metadata: "Optional[dict]" = None) -> None:
    """Record *lesson* under *scope*. No-op when no provider is wired; swallows any provider error."""
    p = _PROVIDER
    if p is None:
        return
    try:
        p.report_lesson(scope, lesson, metadata)
    except Exception:   # noqa: BLE001 — advisory: a lesson-write failure must never break a turn
        pass


def brief(scopes: "Sequence[str]", limit: int = 10) -> str:
    """A scope-PRIORITY merged digest across *scopes* (earlier scopes win). Delegates to the provider's
    ``brief`` when available; otherwise composes deterministically from :func:`get_lessons` over the
    scopes in order (dedup, capped at *limit*). ``""`` when no provider / nothing found / on error."""
    p = _PROVIDER
    if p is None:
        return ""
    try:
        out = p.brief(scopes, limit=limit)
        if isinstance(out, str):
            return out
    except Exception:   # noqa: BLE001 — provider may not implement brief / may raise; compose a digest
        pass
    try:
        seen: set = set()
        merged: List[str] = []
        for sc in scopes:
            for ln in get_lessons(sc, limit=limit):   # already filtered to str items
                if ln not in seen:
                    seen.add(ln)
                    merged.append(ln)
                if len(merged) >= limit:
                    break
            if len(merged) >= limit:
                break
        return "\n".join(merged)
    except Exception:   # noqa: BLE001 — advisory: composing a digest must never break a turn
        return ""


# ─── Redaction-gated promotion — FAIL-CLOSED (AD-9) ───────────────────────────────────────────────
def promote(lesson: str, from_scope: str, to_scope: str,
            *, redactor: "Optional[Callable[[str, str, str], Optional[str]]]" = None) -> str:
    """Promote a project-private *lesson* from *from_scope* to a broader *to_scope* (e.g. curated-global)
    — **only** through a redactor (AD-9). The *redactor* ``(lesson, from_scope, to_scope) -> Optional[str]``
    returns the redacted text to promote, or ``None``/``""`` to REFUSE. **Fail-closed**: a missing /
    non-callable redactor, or a redactor that refuses (or returns a non-string), raises ``ValueError`` —
    a private lesson (which may carry paths/secrets) is never promoted unredacted. On approval the redacted
    lesson is reported under *to_scope* (tagged ``promoted_from``) and returned. (The report itself is
    fail-soft; the *gate* is fail-closed regardless of whether a provider is wired.)"""
    if not callable(redactor):
        raise ValueError("promotion requires a redactor (AD-9: no unredacted cross-scope promotion)")
    redacted = redactor(lesson, from_scope, to_scope)
    if not isinstance(redacted, str) or not redacted.strip():
        raise ValueError("promotion refused by the redaction gate")
    report_lesson(to_scope, redacted, {"promoted_from": from_scope})
    return redacted


# ─── Scope-targeted forget — OPTIONAL provider capability, fail-soft (#601 S14-5 / D5) ────────────
def forget(scope: str) -> bool:
    """Forget every lesson under *scope* — the lesson half of the engine's scope-aware forget endpoint
    (e.g. when a project or track is dropped). This is an **optional** provider capability: if the registered
    provider implements ``forget(scope) -> Any`` it is delegated and a **non-raising call counts as success**
    (returns ``True`` — a forget that returns ``None``/void still happened); with **no provider, or a provider
    without ``forget``, it is a no-op returning ``False``** — byte-identical to the pre-seam engine.
    **Fail-soft** (a provider error is swallowed → ``False``). Kept OFF the required
    :class:`LessonProvider` protocol so a 3-verb provider still satisfies ``isinstance``; #602's backend may
    opt in (duck-typed)."""
    p = _PROVIDER
    if p is None:
        return False
    fn = getattr(p, "forget", None)
    if not callable(fn):
        return False
    try:
        fn(scope)
        return True
    except Exception:   # noqa: BLE001 — advisory: a lesson-forget failure must never break a turn
        return False


__all__ = [
    "__version__",
    "LessonProvider",
    "set_provider",
    "get_provider",
    "get_lessons",
    "report_lesson",
    "brief",
    "promote",
    "forget",
]
