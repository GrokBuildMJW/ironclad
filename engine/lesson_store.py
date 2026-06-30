"""Project-private lesson distiller — a concrete ``ack.lessons`` provider (epic #602 SUB-5).

> **The lesson SEMANTICS behind the #601 seam.** `#601` delivered the curated `ack.lessons` facade
> (a fail-soft no-op until a provider is registered) plus the engine read/write/forget *sites*.
> This module is the one production :class:`~ack.lessons.LessonProvider` the engine registers
> (via :func:`ack.lessons.set_provider`) when ``lessons.enabled`` is on — **OPT-IN, default off**,
> so the default deployment stays byte-identical (no provider wired → the seam is a no-op).

What it adds on top of the string-only seam:
  * **typed distiller categories** (:class:`LessonCategory`: last-failure-reason / best-known-path /
    known-bad-strategy / user-preference) — *provider-internal* (the public seam stays string-only,
    C0 fork-2); in-process consumers (#602's Process-SC / Strategy Revisor) couple to THIS class;
  * **query ranking** (term-overlap) + **recency** ordering in :meth:`get_lessons`;
  * **compaction** — a per-scope cap, oldest dropped first;
  * a **scope-keyed persistent backend** — one JSON file per scope under the installation home,
    keyed by a hash of the engine's opaque ``mem_scope`` partition string;
  * the optional duck-typed :meth:`forget` so the engine's scope-aware forget actually purges lessons.

**Boundary.** Pure stdlib + the project home resolver; it imports NOTHING from ``engine.memory`` /
``engine.warm`` and never touches the Mem0/Valkey keys (the re-homing #601 set out to enforce). The
``scope`` is opaque — this module hashes it for a filesystem-safe name but never parses or mints it.

**Fail-soft.** Every method swallows its own I/O / corruption errors (a missing or corrupt scope file
reads as empty); lessons are advisory and must never break a turn — matching the seam's contract.

**Concurrency.** Read-modify-write is serialized by a process-local lock and each save is atomic
(temp + ``os.replace``). A *cross-process* concurrent write is last-writer-wins (it may drop one
advisory lesson) — acceptable for a hint store; the persistent record is never corrupted.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


class LessonCategory(str, Enum):
    """The typed distiller buckets (provider-internal — NOT on the string-only public seam).

    The default for a plain ``report_lesson`` string with no ``category`` metadata is
    :attr:`GENERAL`; the engine / #602 consumers can record into the specific buckets via
    :meth:`EngineLessonStore.record`.
    """

    LAST_FAILURE_REASON = "last_failure_reason"
    BEST_KNOWN_PATH = "best_known_path"
    KNOWN_BAD_STRATEGY = "known_bad_strategy"
    USER_PREFERENCE = "user_preference"
    GENERAL = "general"


#: Order categories surface in :meth:`EngineLessonStore.brief` (most actionable first).
_BRIEF_ORDER = (
    LessonCategory.LAST_FAILURE_REASON,
    LessonCategory.KNOWN_BAD_STRATEGY,
    LessonCategory.BEST_KNOWN_PATH,
    LessonCategory.USER_PREFERENCE,
    LessonCategory.GENERAL,
)

_WORD_RE = re.compile(r"[a-z0-9]+")


def _scoped(scope: Any) -> bool:
    """A usable partition scope = a non-blank string. A ``None``/non-str/empty/whitespace-only scope has NO
    partition (the read/write methods no-op on it) — so the base/no-project case never touches a hashed file.
    Never raises (a hostile ``str`` subclass whose ``strip()`` blows up → treated as no scope)."""
    try:
        return isinstance(scope, str) and bool(scope.strip())
    except Exception:   # noqa: BLE001 — a hostile scope is treated as no-partition, never raises
        return False


def _safe_cap(value: Any, default: "Optional[int]") -> "Optional[int]":
    """Coerce a compaction cap to ``max(1, int(value))``; on a malformed value return *default* (never
    raises) — so neither construction nor a live ``configure`` can blow up on a bad config value."""
    try:
        return max(1, int(value))
    except Exception:   # noqa: BLE001 — TypeError/ValueError/OverflowError(int(inf)) AND a hostile __int__
        return default


def _tokens(text: str) -> set:
    return set(_WORD_RE.findall(text.lower()))


def _json_safe_meta(meta: Dict[str, Any]) -> Dict[str, Any]:
    """Keep only str-keyed, JSON-serializable metadata entries. A non-serializable value (e.g. an
    ``object()``) is DROPPED rather than persisted — so a direct caller's bad metadata can never make
    :meth:`EngineLessonStore.report_lesson` raise on the later ``json.dumps`` (fail-soft contract)."""
    safe: Dict[str, Any] = {}
    for k, v in meta.items():
        if not isinstance(k, str):
            continue
        try:
            json.dumps(v)
        except (TypeError, ValueError):
            continue
        safe[k] = v
    return safe


def _coerce_category(value: Any) -> LessonCategory:
    """Map a metadata ``category`` value to a :class:`LessonCategory`; unknown / missing / hostile → GENERAL.
    Never raises (the isinstance check is inside the guard too — a hostile ``__class__`` cannot escape)."""
    try:
        if isinstance(value, LessonCategory):
            return value
        return LessonCategory(str(value))
    except Exception:   # noqa: BLE001 — hostile __str__/__class__: never break a never-raises caller
        return LessonCategory.GENERAL


class EngineLessonStore:
    """A scope-partitioned, persistent :class:`~ack.lessons.LessonProvider` (duck-typed; it also
    implements the optional ``forget``). Construct with the base directory the JSON files live under
    (production: ``ironclad_home()/lessons``) and a per-scope compaction cap."""

    def __init__(self, base_dir: "Path | str", *, max_per_scope: int = 200) -> None:
        try:
            self._base = Path(base_dir)
        except Exception:   # noqa: BLE001 — a hostile/invalid base_dir → INERT store (no path resolves, all
            self._base = None   # I/O no-ops via the per-method guards); never raises, never creates a dir
        self._max = _safe_cap(max_per_scope, 200)
        self._lock = threading.Lock()

    def configure(self, *, max_per_scope: int) -> None:
        """Update the live compaction cap (so a runtime ``/config set lessons.max_per_scope`` takes effect
        on the already-registered store, not just on the next boot). A malformed cap is ignored (the
        previous cap is kept) — this is a callable seam and must never raise to its caller."""
        new = _safe_cap(max_per_scope, None)
        if new is not None:
            self._max = new

    # ─── persistence (scope → one JSON file; opaque scope hashed to a safe name) ──────────────────
    def _path(self, scope: str) -> Path:
        digest = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:32]
        return self._base / f"{digest}.json"

    def _load(self, scope: str) -> Dict[str, Any]:
        """Read a scope's record, fail-soft → a fresh empty record on any miss / corruption."""
        fresh = {"scope": scope, "next_seq": 1, "lessons": []}
        try:
            raw = self._path(scope).read_text(encoding="utf-8")
        except Exception:   # noqa: BLE001 — fail-soft: missing/permission AND invalid-UTF-8 (UnicodeDecodeError
            return fresh    # is a ValueError, not OSError) → a corrupt-bytes file must read as empty, not raise
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return fresh
        if not isinstance(data, dict) or not isinstance(data.get("lessons"), list):
            return fresh
        # keep only well-formed records (a hand-corrupted entry is dropped, never raised on)
        good: List[Dict[str, Any]] = []
        for rec in data["lessons"]:
            if isinstance(rec, dict) and isinstance(rec.get("text"), str) and isinstance(rec.get("seq"), int):
                good.append(rec)
        data["lessons"] = good
        if not isinstance(data.get("next_seq"), int):
            data["next_seq"] = (max((r["seq"] for r in good), default=0) + 1)
        data["scope"] = scope
        return data

    def _save(self, scope: str, data: Dict[str, Any]) -> None:
        """Atomically persist *data* (temp + ``os.replace``); fail-soft on any I/O error."""
        tmp = None
        try:
            path = self._path(scope)   # inside the try: an inert (None-base) store no-ops here too
            self._base.mkdir(parents=True, exist_ok=True)
            tmp = path.parent / f"{path.name}.{os.getpid()}.tmp"
            # metadata is already JSON-sanitized; (TypeError, ValueError) is a defense-in-depth backstop so
            # serialization can NEVER raise to a direct provider caller (fail-soft).
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
        except Exception:   # noqa: BLE001 — I/O / serialization / an inert (None-base) store → no-op, never raises
            # a failed write/replace must not orphan the temp file — best-effort cleanup (os.replace, on
            # success, consumes tmp; this only fires when write/replace itself failed).
            if tmp is not None:
                try:
                    tmp.unlink()
                except OSError:
                    pass

    # ─── LessonProvider — the 3 required verbs + the optional forget ──────────────────────────────
    def report_lesson(self, scope: str, lesson: str, metadata: "Optional[dict]" = None) -> None:
        """Record *lesson* under *scope* (category from ``metadata['category']``, else GENERAL).
        An exact (text, category) duplicate is refreshed (recency bumped) rather than re-appended;
        over the cap, the oldest is dropped. No-op on an empty scope/lesson; never raises."""
        try:
            if not (_scoped(scope) and isinstance(lesson, str) and lesson.strip()):
                return
            meta = dict(metadata) if isinstance(metadata, dict) else {}
            category = _coerce_category(meta.get("category"))
            meta = _json_safe_meta(meta)   # drop non-serializable entries so json.dumps can't raise later
            text = lesson.strip()
            with self._lock:
                data = self._load(scope)
                seq = int(data["next_seq"])
                lessons: List[Dict[str, Any]] = data["lessons"]
                for rec in lessons:
                    if rec["text"] == text and rec.get("category") == category.value:
                        rec["seq"] = seq                       # refresh recency of an exact duplicate
                        break
                else:
                    lessons.append({"seq": seq, "text": text, "category": category.value, "meta": meta})
                data["next_seq"] = seq + 1
                if len(lessons) > self._max:                   # compaction: drop the oldest (lowest seq)
                    lessons.sort(key=lambda r: r["seq"])
                    del lessons[: len(lessons) - self._max]
                self._save(scope, data)
        except Exception:   # noqa: BLE001 — absolute fail-soft: a hostile lesson/scope/meta never raises
            return

    def record(self, scope: str, lesson: str, category: "LessonCategory | str",
               metadata: "Optional[dict]" = None) -> None:
        """Typed convenience for in-process consumers (#602 SUB-6/7): record into a specific
        :class:`LessonCategory`. Delegates to :meth:`report_lesson` with the category in metadata. Fail-soft —
        never raises (a hostile category/metadata is swallowed; report_lesson is itself fully guarded)."""
        try:
            cat = category if isinstance(category, LessonCategory) else _coerce_category(category)
            meta = dict(metadata) if isinstance(metadata, dict) else {}
            meta["category"] = cat.value
            self.report_lesson(scope, lesson, meta)
        except Exception:   # noqa: BLE001 — absolute fail-soft for the typed convenience too
            return

    def _ranked(self, scope: str, query: str = "") -> List[Dict[str, Any]]:
        data = self._load(scope)
        lessons = list(data["lessons"])
        q = _tokens(query) if query else set()
        if q:
            # term-overlap score desc, then recency (seq) desc — deterministic + stable.
            lessons.sort(key=lambda r: (len(q & _tokens(r["text"])), r["seq"]), reverse=True)
        else:
            lessons.sort(key=lambda r: r["seq"], reverse=True)   # recency desc
        return lessons

    def get_lessons(self, scope: str, query: str = "", limit: int = 10) -> List[str]:
        """Up to *limit* lesson strings for *scope*, ranked by *query* term-overlap (else recency)."""
        if not _scoped(scope):
            return []
        try:
            return [r["text"] for r in self._ranked(scope, query or "")[: max(0, int(limit))]]
        except Exception:   # noqa: BLE001 — advisory: a read failure must never break a turn
            return []

    def by_category(self, scope: str, category: "LessonCategory | str", limit: int = 10) -> List[str]:
        """Typed read for in-process consumers: the lessons in one category (recency desc)."""
        if not _scoped(scope):   # blank scope ⇒ no partition (matches get_lessons)
            return []
        try:
            cat = category if isinstance(category, LessonCategory) else _coerce_category(category)
            return [r["text"] for r in self._ranked(scope) if r.get("category") == cat.value][: max(0, int(limit))]
        except Exception:   # noqa: BLE001 — advisory
            return []

    def brief(self, scopes: "Sequence[str]", limit: int = 10) -> str:
        """A scope-PRIORITY digest across *scopes* (earlier scopes win), grouped by category in
        actionability order, deduped by text and capped at *limit*. ``""`` when nothing found."""
        try:
            seen: set = set()
            by_cat: Dict[LessonCategory, List[str]] = {c: [] for c in _BRIEF_ORDER}
            picked = 0
            for sc in scopes or ():
                if picked >= limit:
                    break
                if not _scoped(sc):   # blank scope has no partition (matches get_lessons)
                    continue
                for rec in self._ranked(sc):
                    text = rec["text"]
                    if text in seen:
                        continue
                    seen.add(text)
                    by_cat[_coerce_category(rec.get("category"))].append(text)
                    picked += 1
                    if picked >= limit:
                        break
            out: List[str] = []
            for cat in _BRIEF_ORDER:
                items = by_cat[cat]
                if not items:
                    continue
                out.append(f"[{cat.value}]")
                out.extend(f"- {t}" for t in items)
            return "\n".join(out)
        except Exception:   # noqa: BLE001 — advisory: composing a digest must never break a turn
            return ""

    def forget(self, scope: str) -> bool:
        """Delete every lesson under *scope* (the optional provider verb the engine's scope-aware forget
        delegates to). Returns ``True`` if a file was removed. Fail-soft; never raises."""
        if not _scoped(scope):
            return False
        with self._lock:
            try:
                self._path(scope).unlink()
                return True
            except Exception:   # noqa: BLE001 — missing file / I/O / any path hiccup → not removed, never raises
                return False
