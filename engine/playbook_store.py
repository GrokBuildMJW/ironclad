"""ACE PlaybookStore — the always-on loop-intelligence backend (epic #855 ACE-WIRE / #863).

This is the engine-side store that **supersedes** the #602 ``EngineLessonStore`` as the registered
``ack.lessons`` provider (operator decision 2026-06-30: ACE is the core, always-on mechanic — no enable
toggle). It is a drop-in for the lesson seam in two senses at once:

  * the **string** ``ack.lessons.LessonProvider`` protocol (``get_lessons`` / ``report_lesson`` /
    ``brief``) the engine read/write sites + the curated facade use; and
  * the **typed** ``record`` / ``by_category`` / ``forget`` / ``configure`` surface the in-process #602
    consumers (Process-SC / Strategy Revisor) couple to — so ``_concrete_lesson_provider`` keeps
    returning a usable provider (duck-typed) and **Process-SC never silently breaks**.

On top of the lesson seam it is the **ACE engine**: it wraps one :class:`ack.ace.Playbook` per opaque
``mem_scope`` (one JSON file under ``ironclad_home()/ace_playbooks``), and exposes the ACE-native
operations — :meth:`context_for` (query-aware relevant-bullet retrieval, the 32k-safe Generator read)
and :meth:`adapt` (the online reflect→curate→refine step, run OFF the hot path by the engine's
``ReflectionWorker``). The orchestrator-model ``chat``, the ``/embed`` ``embed`` adapter and the token
``budget`` are **injected** (mirroring ``ack.verify``) so this module stays engine-pure: stdlib + the
pure ``ack.ace`` package only, importing nothing from ``engine.memory`` / ``engine.warm`` / the Mem0
keys (the #601 re-homing boundary).

**Fail-soft.** Every method swallows its own I/O / transport / parse errors — lessons are advisory and
must never break a turn. With no ``chat`` injected (e.g. a wheel with no orchestrator model) :meth:`adapt`
is a no-op and the playbook simply stays empty; reads keep working.

**Concurrency.** Read-modify-write is serialized by a per-scope-store lock; each save is atomic (temp +
``os.replace``). A cross-process concurrent write is last-writer-wins — acceptable for an advisory store.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from ack.ace import (
    Playbook,
    Bullet,
    DEFAULT_SECTIONS,
    AdaptConfig,
    Trajectory,
    select_relevant,
    prune,
)

# The category the engine's Process-SC writes couple to lives in the sibling module; reuse the enum so a
# typed record() round-trips exactly. Imported lazily-safe (sibling, stdlib-only) at module load.
try:
    from lesson_store import LessonCategory   # bare engine-sibling import (like project_registry)
except Exception:   # noqa: BLE001 — keep the store importable even if the sibling is unavailable
    LessonCategory = None   # type: ignore[assignment]

#: Where a #602 typed category lands in the ACE playbook. Every legacy lesson is strategic by default; the
#: richer sectioning (apis / verification / formulas) is populated by ACE's own reflect→curate.
_DEFAULT_SECTION = "strategies_and_hard_rules"
_CATEGORY_SECTION = {
    "known_bad_strategy":  "strategies_and_hard_rules",
    "last_failure_reason": "strategies_and_hard_rules",
    "best_known_path":     "strategies_and_hard_rules",
    "user_preference":     "strategies_and_hard_rules",
    "general":             "strategies_and_hard_rules",
}
_VALID_SECTIONS = set(DEFAULT_SECTIONS)
_HISTORY_MAX = 20                       # #1082: bound the per-scope rollback history (newest kept)
_WORD_RE = re.compile(r"[a-z0-9]+")


def _scoped(scope: Any) -> bool:
    """A usable partition = a non-blank string (matches EngineLessonStore). Never raises."""
    try:
        return isinstance(scope, str) and bool(scope.strip())
    except Exception:   # noqa: BLE001 — a hostile scope is treated as no-partition
        return False


def _safe_cap(value: Any, default: "Optional[int]") -> "Optional[int]":
    """Coerce a cap to ``max(1, int(value))``; a malformed value → *default* (never raises)."""
    try:
        return max(1, int(value))
    except Exception:   # noqa: BLE001 — TypeError/ValueError/OverflowError(int(inf)) / hostile __int__
        return default


def _category_value(category: Any) -> str:
    """Map a #602 category (enum / str / hostile) to its string value; unknown → ``"general"``."""
    try:
        if LessonCategory is not None and isinstance(category, LessonCategory):
            return category.value
        v = str(category).strip().lower()
        return v if v in _CATEGORY_SECTION else "general"
    except Exception:   # noqa: BLE001 — never break a never-raises caller
        return "general"


def _section_for(category_value: str) -> str:
    return _CATEGORY_SECTION.get(category_value, _DEFAULT_SECTION)


class PlaybookStore:
    """A scope-partitioned, persistent ACE playbook that doubles as the engine's always-on lesson provider.
    Construct with the base directory the per-scope JSON files live under (production:
    ``ironclad_home()/ace_playbooks``); inject the transports (``chat`` / ``embed`` / ``budget``) the online
    adaptation needs. ``max_bullets`` caps a scope's playbook (the 32k-window guard, #366)."""

    def __init__(self, base_dir: "Path | str", *, chat: "Optional[Callable[[str], str]]" = None,
                 embed: "Optional[Callable[[List[str]], List[List[float]]]]" = None, budget: Any = None,
                 max_bullets: int = 200, config: "Optional[AdaptConfig]" = None) -> None:
        try:
            self._base = Path(base_dir)
        except Exception:   # noqa: BLE001 — a hostile base_dir → INERT store (I/O no-ops via per-method guards)
            self._base = None
        self._chat = chat
        self._embed = embed
        self._budget = budget
        self._max = _safe_cap(max_bullets, 200)
        self._config = config or AdaptConfig(max_bullets=self._max)
        self._top_k = 8                                # #905: the context_for injection cap (`ace.top_k`); live-configurable
        self._eval_fn: "Optional[Callable[[Playbook], Any]]" = None   # injected quality scorer (higher=better)
        self._lock = threading.Lock()

    # ─── transport injection (the engine wires these after building the /embed + chat adapters) ───────
    def set_transports(self, *, chat: Any = None, embed: Any = None, budget: Any = None,
                       eval_fn: Any = None) -> None:
        """Inject (or replace) the online-adaptation transports on the live store. ``None`` leaves a
        transport unchanged (pass an explicit sentinel only via the kwargs you mean to set). *eval_fn*
        is the injected quality scorer (a playbook → a numeric score, higher=better) the transactional
        promotion gate uses; a deployment wires it (a held-out eval / a telemetry-derived signal)."""
        if chat is not None:
            self._chat = chat
        if embed is not None:
            self._embed = embed
        if budget is not None:
            self._budget = budget
        if eval_fn is not None:
            self._eval_fn = eval_fn

    def configure(self, *, max_bullets: Any = None, max_per_scope: Any = None,
                  top_k: Any = None) -> None:
        """Live cap change (so a runtime ``/config set`` takes effect on the registered store). Accepts
        ``max_per_scope`` as a back-compat alias for the EngineLessonStore key. ``top_k`` sets the
        ``context_for`` injection cap (`ace.top_k`, #905). Malformed values ⇒ kept."""
        raw = max_bullets if max_bullets is not None else max_per_scope
        new = _safe_cap(raw, None)
        if new is not None:
            self._max = new
            self._config.max_bullets = new
        if top_k is not None:
            try:
                self._top_k = max(0, int(top_k))
            except (TypeError, ValueError):           # a malformed top_k is kept (no silent 0)
                pass

    # ─── persistence (scope → one JSON file; opaque scope hashed to a safe name) ──────────────────────
    def _path(self, scope: str) -> Path:
        digest = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:32]
        return self._base / f"{digest}.json"

    def _load(self, scope: str) -> Playbook:
        """Read a scope's playbook, fail-soft → a fresh empty Playbook on any miss / corruption / a
        newer-schema file (``from_json`` refuses a future version → we treat it as empty rather than crash)."""
        try:
            raw = self._path(scope).read_text(encoding="utf-8")
        except Exception:   # noqa: BLE001 — missing / permission / invalid-UTF-8 → empty, never raises
            return Playbook()
        try:
            return Playbook.from_json(raw)
        except Exception:   # noqa: BLE001 — corrupt / newer-schema → empty (advisory store, never raises)
            return Playbook()

    def _save(self, scope: str, pb: Playbook) -> bool:
        """Atomically persist *pb* (temp + ``os.replace``); fail-soft on any I/O / serialization error."""
        tmp = None
        try:
            path = self._path(scope)   # an inert (None-base) store raises here → caught below (no-op)
            self._base.mkdir(parents=True, exist_ok=True)
            tmp = path.parent / f"{path.name}.{os.getpid()}.tmp"
            tmp.write_text(pb.to_json(), encoding="utf-8")
            os.replace(tmp, path)
            return True
        except Exception:   # noqa: BLE001 — I/O / serialization / inert store → no-op, never raises
            if tmp is not None:
                try:
                    tmp.unlink()
                except OSError:
                    pass
            return False

    # ─── #1082: operator-facing playbook safety — versioning (M-002) + selective forget (Q-001) ───────
    # The learned playbook adapts silently; these give an operator a rollback net (snapshot/rollback) and a
    # scalpel (unlearn a bad bullet) via `/ace`. The version log persists next to the scope playbook so a
    # rollback point survives the session; every mutating verb snapshots first, so it is itself reversible.
    def _history_path(self, scope: str) -> Path:
        return self._path(scope).with_suffix(".history.json")

    def _load_history(self, scope: str):
        from ack.ace.robust import PlaybookHistory
        try:
            log = json.loads(self._history_path(scope).read_text(encoding="utf-8"))
            return PlaybookHistory.from_log(log)
        except Exception:   # noqa: BLE001 — missing / corrupt → empty history, never raises
            return PlaybookHistory()

    def _save_history(self, scope: str, hist) -> bool:
        tmp = None
        try:
            path = self._history_path(scope)
            self._base.mkdir(parents=True, exist_ok=True)
            log = hist.to_log()[-_HISTORY_MAX:]          # keep only the newest snapshots
            tmp = path.parent / f"{path.name}.{os.getpid()}.tmp"
            tmp.write_text(json.dumps(log), encoding="utf-8")
            os.replace(tmp, path)
            return True
        except Exception:   # noqa: BLE001 — advisory, never raises
            if tmp is not None:
                try:
                    tmp.unlink()
                except OSError:
                    pass
            return False

    def _quarantine_path(self, scope: str) -> Path:
        return self._path(scope).with_suffix(".quarantine.json")

    def _quarantine(self, scope: str, candidate: Playbook, source_version: str, *,
                    state: str, reason: str, scores: "Optional[dict]" = None) -> bool:
        """Atomically retain a bounded candidate record without changing the active playbook."""
        tmp = None
        try:
            path = self._quarantine_path(scope)
            self._base.mkdir(parents=True, exist_ok=True)
            try:
                log = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(log, list):
                    log = []
            except Exception:   # noqa: BLE001 — missing/corrupt quarantine starts a fresh bounded log
                log = []
            log.append({
                "scope_hash": hashlib.sha256(scope.encode("utf-8")).hexdigest()[:32],
                "source_version": source_version,
                "candidate": json.loads(candidate.to_json()),
                "state": state,
                "reason": reason,
                "scores": scores,
                "timestamp": time.time(),
            })
            tmp = path.parent / f"{path.name}.{os.getpid()}.tmp"
            tmp.write_text(json.dumps(log[-_HISTORY_MAX:]), encoding="utf-8")
            os.replace(tmp, path)
            return True
        except Exception:   # noqa: BLE001 — quarantine is advisory; active state remains untouched
            if tmp is not None:
                try:
                    tmp.unlink()
                except OSError:
                    pass
            return False

    def quarantined(self, scope: str) -> list:
        """Return retained candidate records for *scope* (oldest→newest); fail-soft → ``[]``."""
        if not _scoped(scope):
            return []
        try:
            log = json.loads(self._quarantine_path(scope).read_text(encoding="utf-8"))
            return log if isinstance(log, list) else []
        except Exception:   # noqa: BLE001 — missing/corrupt quarantine is an empty operator view
            return []

    def snapshot(self, scope: str) -> dict:
        """Record the current scope playbook as a named rollback point. Returns ``{version, versions}`` or
        ``{error}``. Idempotent (a snapshot of an unchanged tip returns the same id)."""
        if not _scoped(scope):
            return {"error": "no active scope"}
        try:
            with self._lock:
                hist = self._load_history(scope)
                vid = hist.snapshot(self._load(scope))
                self._save_history(scope, hist)
                return {"version": vid, "versions": len(hist.versions())}
        except Exception as ex:   # noqa: BLE001
            return {"error": repr(ex)}

    def versions(self, scope: str) -> List[str]:
        """The recorded snapshot version ids for *scope* (oldest→newest)."""
        if not _scoped(scope):
            return []
        try:
            return self._load_history(scope).versions()
        except Exception:   # noqa: BLE001
            return []

    def rollback(self, scope: str, target: "Optional[str]" = None) -> dict:
        """Restore the scope playbook to *target* (or the previous snapshot). Snapshots the CURRENT state
        first so the rollback is itself reversible, then persists the restored playbook. Returns
        ``{rolled_back_to, size}`` or ``{error}``."""
        if not _scoped(scope):
            return {"error": "no active scope"}
        try:
            with self._lock:
                hist = self._load_history(scope)
                hist.snapshot(self._load(scope))          # capture current → reversible
                restored = hist.rollback(target)
                if restored is None:
                    self._save_history(scope, hist)
                    return {"error": "no such version / nothing earlier to roll back to"}
                # #1551: the persistence helpers fail-soft to False on an I/O error (read-only ACE dir / full
                # disk). If the restored playbook did not actually reach disk, the harmful active JSON is
                # UNCHANGED — surface an error instead of a false success that tells the operator the unsafe
                # guidance was removed.
                if not self._save(scope, restored):
                    return {"error": "rollback could not persist the restored playbook — the active playbook "
                                     "is unchanged (read-only ACE dir or full disk?)"}
                vid = hist.snapshot(restored)             # the restored state is the new tip
                if not self._save_history(scope, hist):
                    return {"error": "rollback restored the active playbook but could not persist its version "
                                     "history"}
                return {"rolled_back_to": vid, "size": len(restored)}
        except Exception as ex:   # noqa: BLE001
            return {"error": repr(ex)}

    def unlearn(self, scope: str, bullet_ids) -> dict:
        """Selectively remove bullets by id from the scope playbook (snapshots first, so it is reversible).
        Returns ``{removed, missing}`` or ``{error}``."""
        if not _scoped(scope):
            return {"error": "no active scope"}
        try:
            from ack.ace.robust import unlearn as _unlearn
            with self._lock:
                pb = self._load(scope)
                hist = self._load_history(scope)
                hist.snapshot(pb)                         # reversible
                res = _unlearn(pb, bullet_ids)
                if res.get("removed"):
                    self._save(scope, pb)
                    hist.snapshot(pb)
                self._save_history(scope, hist)
                return res
        except Exception as ex:   # noqa: BLE001
            return {"error": repr(ex)}

    # ─── internal: a cheap, hot-path-safe sync write (no LLM, lexical prune only) ─────────────────────
    def _add(self, scope: str, content: str, *, section: str, tags: "Sequence[str]" = ()) -> "Optional[str]":
        text = (content or "").strip()
        sect = section if section in _VALID_SECTIONS else _DEFAULT_SECTION
        if not (_scoped(scope) and text):
            return None
        try:
            with self._lock:
                pb = self._load(scope)
                for b in pb.section_bullets(sect):          # exact-duplicate guard (bump utility, don't grow)
                    if b.content == text:
                        b.mark_helpful()
                        for t in tags:
                            b.add_tag(t)
                        self._save(scope, pb)
                        return b.id
                bullet = pb.add_bullet(text, sect, tags=list(tags))
                if self._max and len(pb) > self._max:        # bound the playbook (32k guard) — lexical/utility prune
                    prune(pb, max_bullets=self._max)
                self._save(scope, pb)
                return bullet.id
        except Exception:   # noqa: BLE001 — absolute fail-soft: a hostile lesson/scope never raises
            return None

    # ─── ack.lessons.LessonProvider — the string protocol the engine + facade call ────────────────────
    def report_lesson(self, scope: str, lesson: str, metadata: "Optional[dict]" = None) -> None:
        """Record *lesson* as a bullet (category from ``metadata['category']``, else GENERAL → its mapped
        section, tagged with the category). Cheap + synchronous (no LLM) — the ACE reflect→curate enrichment
        runs separately on the worker. No-op on an empty scope/lesson; never raises."""
        meta = metadata if isinstance(metadata, dict) else {}
        cat = _category_value(meta.get("category"))
        self._add(scope, lesson, section=_section_for(cat), tags=(cat,))

    def get_lessons(self, scope: str, query: str = "", limit: int = 10) -> List[str]:
        """Up to *limit* bullet contents for *scope*, ranked by *query* relevance (semantic via the injected
        embedder, lexical fallback) — falls back to **net_utility** (helpful−harmful, recency as a tiebreak)
        when *query* is empty (C2 #906 — the docstring previously said "recency"). Advisory; never raises."""
        if not _scoped(scope):
            return []
        try:
            pb = self._load(scope)
            n = max(0, int(limit))
            if not n:
                return []
            if (query or "").strip():
                return [b.content for b in select_relevant(pb, query, embed=self._embed, top_k=n)]
            return [b.content for b in self._by_utility(pb)[:n]]
        except Exception:   # noqa: BLE001 — a read failure must never break a turn
            return []

    def brief(self, scopes: "Sequence[str]", limit: int = 10) -> str:
        """A scope-PRIORITY digest across *scopes* (earlier scopes win), deduped by content and capped at
        *limit*, grouped by section. ``""`` when nothing found. The non-query handover/pre-turn injection."""
        try:
            bullets = self._collect(scopes, query="", limit=limit)
            return _render_bullets(bullets)
        except Exception:   # noqa: BLE001 — composing a digest must never break a turn
            return ""

    # ─── typed surface — the in-process #602 consumers (Process-SC) couple to these ───────────────────
    def record(self, scope: str, lesson: str, category: "Any", metadata: "Optional[dict]" = None) -> None:
        """Typed write for in-process consumers (#602 SUB-6/7): record into a specific category. Lands as a
        bullet in the category's mapped section, tagged with the category value. Fail-soft — never raises."""
        cat = _category_value(category)
        self._add(scope, lesson, section=_section_for(cat), tags=(cat,))

    def by_category(self, scope: str, category: "Any", limit: int = 10) -> List[str]:
        """Typed read: bullet contents tagged with *category* (recency desc). Matches EngineLessonStore."""
        if not _scoped(scope):
            return []
        try:
            cat = _category_value(category)
            hits = [b for b in reversed(self._load(scope).bullets()) if cat in b.tags]
            return [b.content for b in hits[: max(0, int(limit))]]
        except Exception:   # noqa: BLE001 — advisory
            return []

    def forget(self, scope: str) -> bool:
        """Delete EVERY persisted artifact for a scope — the active playbook AND its version-history
        (`.history.json`) and quarantined-candidate (`.quarantine.json`) side files — so a forgotten lesson
        cannot be recovered via ``versions()``/``rollback()`` (#1552). Returns True iff at least one file was
        removed. Fail-soft; never raises."""
        if not _scoped(scope):
            return False
        with self._lock:
            removed = False
            for path in (self._path(scope), self._history_path(scope), self._quarantine_path(scope)):
                try:
                    path.unlink()
                    removed = True
                except Exception:   # noqa: BLE001 — missing file / I/O → skip, never raises
                    pass
            return removed

    # ─── ACE engine — query-aware retrieval (the Generator read) + the online adaptation step ─────────
    def context_for(self, scopes: "Sequence[str]", *, query: str, limit: "Optional[int]" = None) -> str:
        """Query-aware relevant-bullet injection (the 32k-safe Generator read, H-001/N-002). Selects the
        *limit* bullets most relevant to *query* across *scopes* (semantic via the injected embedder, lexical
        fallback), scope-priority, deduped + rendered with ids so the agent can cite what it used. ``""`` when
        none. *limit* ``None`` uses the configured ``top_k`` (``ace.top_k``, #905). This is what the handover
        read-site injects when an ACE provider is wired. Never raises."""
        try:
            n = limit if limit is not None else self._top_k
            bullets = self._collect(scopes, query=query or "", limit=n)
            return _render_bullets(bullets)
        except Exception:   # noqa: BLE001 — advisory: a context read must never break a turn
            return ""

    def adapt(self, trajectory: "Trajectory", *, scope: str) -> dict:
        """One online adaptation step over *scope*'s playbook (reflect→curate→apply→refine, budget-gated),
        transactionally promoted or quarantined. Run OFF the hot path by the engine's ReflectionWorker. No-op
        (no charge, no mutation) when no ``chat`` transport is injected or nothing is learned. Fail-soft —
        never raises; returns a summary."""
        from ack.ace import adapt_once   # local import keeps the per-method surface tidy
        base = {"skipped": True, "added": 0, "rated": 0, "merged": 0, "pruned": 0}
        if self._chat is None or not _scoped(scope):
            return base
        try:
            with self._lock:
                active = self._load(scope)
                try:
                    hist = self._load_history(scope)
                    source_version = hist.snapshot(active)
                    if not self._save_history(scope, hist):
                        return {**base, "snapshot_failed": True}
                except Exception:   # noqa: BLE001 — no durable rollback point ⇒ refuse before adaptation
                    return {**base, "snapshot_failed": True}

                candidate = Playbook.from_json(active.to_json())
                summary = adapt_once(trajectory, candidate, chat=self._chat, embed=self._embed,
                                     budget=self._budget, config=self._config)
                if summary.get("skipped"):
                    return summary

                if self._eval_fn is not None:
                    try:
                        from ack.ace.robust import regression_verdict
                        before_score = self._eval_fn(active)
                        after_score = self._eval_fn(candidate)
                        if (isinstance(before_score, bool) or not isinstance(before_score, (int, float))
                                or isinstance(after_score, bool) or not isinstance(after_score, (int, float))
                                or not math.isfinite(before_score) or not math.isfinite(after_score)):
                            raise TypeError("evaluator returned a non-numeric or non-finite score")
                    except Exception:   # noqa: BLE001 — unavailable evaluation never authorizes promotion
                        self._quarantine(scope, candidate, source_version, state="unpromoted",
                                         reason="evaluation unavailable")
                        summary["promoted"] = False
                        summary["quarantined"] = True
                        return summary

                    verdict = regression_verdict(before_score, after_score)
                    scores = {"before": verdict["before"], "after": verdict["after"]}
                    summary["scores"] = scores
                    if verdict["revert"]:
                        self._quarantine(scope, candidate, source_version, state="regression",
                                         reason="measured regression", scores=scores)
                        summary["promoted"] = False
                        summary["quarantined"] = True
                        return summary
                else:
                    active_n = len(active)
                    cand_n = len(candidate)
                    destructive = ((cand_n == 0 and active_n > 0)
                                   or (active_n > 0 and cand_n < active_n * 0.5))
                    if destructive:
                        self._quarantine(
                            scope, candidate, source_version, state="destructive",
                            reason="catastrophic playbook loss under a non-evaluated adaptation",
                        )
                        summary["promoted"] = False
                        summary["quarantined"] = True
                        return summary

                if not self._save(scope, candidate):
                    return {**summary, "skipped": True, "promoted": False, "promotion_failed": True}
                hist.snapshot(candidate)
                self._save_history(scope, hist)
                summary["promoted"] = True
                return summary
        except Exception:   # noqa: BLE001 — an adaptation hiccup must never kill the worker
            return base

    def warmup(self, scope: str, trajectories: "Sequence[Any]", *, max_epochs: int = 1) -> dict:
        """Offline warm-start (#915, G-004): batch-replay past *trajectories* to SEED *scope*'s playbook, which
        the online loop then continues on. No-op (no charge, no mutation) without a ``chat`` transport. One
        epoch by default (a warm build is several LLM calls). Persisted iff something was learned. Fail-soft —
        never raises; returns the offline build report."""
        from ack.ace import warmup as _offline_warmup, OfflineConfig   # local import keeps the surface tidy
        base = {"skipped": True, "added": 0, "rated": 0, "pruned": 0, "samples_seen": 0}
        if self._chat is None or not _scoped(scope):
            return base
        try:
            with self._lock:
                pb = self._load(scope)
                report = _offline_warmup(list(trajectories or []), pb, chat=self._chat, embed=self._embed,
                                         budget=self._budget, config=OfflineConfig(max_epochs=max(1, int(max_epochs))))
                if report.get("added") or report.get("rated"):
                    self._save(scope, pb)
                return report
        except Exception:   # noqa: BLE001 — a warm-start hiccup must never break the command/loop
            return base

    def benchmark(self, trajectories: "Sequence[Any]") -> dict:
        """Opt-in efficiency DIAGNOSTIC (#918): run `compare_adaptation` over past *trajectories* (as Samples)
        to measure whether ACE delivers the paper's J-001 (no full-rewrites) + J-002 (>50% fewer rollouts than
        evolutionary) gains vs the monolithic baselines. Each strategy builds its OWN playbook — the live one
        is NEVER mutated (pure measurement). No-op without a chat transport. Fail-soft; returns the report."""
        from ack.ace import compare_adaptation, Sample   # local import keeps the surface tidy
        if self._chat is None:
            return {"skipped": True}
        try:
            samples = [Sample(query=(getattr(t, "query", "") or ""), trajectory=t) for t in (trajectories or [])]
            if not samples:
                return {"skipped": True, "reason": "no trajectories"}
            return compare_adaptation(samples, chat=self._chat, embed=self._embed)
        except Exception:   # noqa: BLE001 — a diagnostic must never break the command
            return {"skipped": True}

    # ─── helpers ──────────────────────────────────────────────────────────────────────────────────────
    @staticmethod
    def _by_utility(pb: Playbook) -> List[Bullet]:
        """All bullets, most useful first (net_utility desc), recency as a stable tiebreak (newest first)."""
        recent_first = list(reversed(pb.bullets()))   # bullets() is oldest→newest; stable-sort keeps recency
        recent_first.sort(key=lambda b: b.net_utility, reverse=True)
        return recent_first

    def _collect(self, scopes: "Sequence[str]", *, query: str, limit: int) -> List[Bullet]:
        """Scope-priority bullet selection (earlier scopes win), deduped by content, capped at *limit*.
        Query-aware via :func:`select_relevant` when *query* is non-empty, else utility/recency ranked."""
        n = max(0, int(limit))
        if not n:
            return []
        seen: set = set()
        out: List[Bullet] = []
        for sc in scopes or ():
            if len(out) >= n:
                break
            if not _scoped(sc):
                continue
            pb = self._load(sc)
            ranked = (select_relevant(pb, query, embed=self._embed, top_k=n) if query.strip()
                      else self._by_utility(pb))
            for b in ranked:
                if b.content in seen:
                    continue
                seen.add(b.content)
                out.append(b)
                if len(out) >= n:
                    break
        return out


def _render_bullets(bullets: "Sequence[Bullet]") -> str:
    """Render a bounded bullet list grouped by section: a ``[section]`` header then ``- [id] content #tags``
    lines (so the Generator can cite used bullets, H-002). ``""`` for an empty list."""
    if not bullets:
        return ""
    order: List[str] = list(DEFAULT_SECTIONS)
    by_sect: Dict[str, List[Bullet]] = {}
    for b in bullets:
        by_sect.setdefault(b.section, []).append(b)
        if b.section not in order:
            order.append(b.section)
    lines: List[str] = []
    for sect in order:
        items = by_sect.get(sect)
        if not items:
            continue
        lines.append(f"[{sect}]")
        for b in items:
            tags = " ".join(f"#{t}" for t in b.tags)
            lines.append(f"- [{b.id}] {b.content}" + (f"  {tags}" if tags else ""))
    return "\n".join(lines)


def migrate_lessons(old_base: "Path | str", store: PlaybookStore) -> int:
    """One-time, best-effort migration of an existing #602 ``EngineLessonStore`` tree into *store*: each
    legacy per-scope JSON (which embeds its opaque ``scope`` + typed ``lessons``) is replayed through
    :meth:`PlaybookStore.record` so nothing is lost when ACE supersedes the string store. Idempotent at the
    bullet level (``_add`` dedupes exact content). Returns the number of lessons migrated. Fail-soft — a
    missing/corrupt legacy tree migrates what it can and never raises."""
    migrated = 0
    try:
        base = Path(old_base)
    except Exception:   # noqa: BLE001 — a hostile path → nothing to migrate
        return 0
    try:
        files = list(base.glob("*.json")) if base.exists() else []
    except Exception:   # noqa: BLE001 — I/O on the dir → nothing
        return 0
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:   # noqa: BLE001 — skip a corrupt legacy file
            continue
        if not isinstance(data, dict):
            continue
        scope = data.get("scope")
        if not _scoped(scope):
            continue
        for rec in (data.get("lessons") or []):
            if not (isinstance(rec, dict) and isinstance(rec.get("text"), str)):
                continue
            store.record(scope, rec["text"], rec.get("category", "general"),
                         rec.get("meta") if isinstance(rec.get("meta"), dict) else None)
            migrated += 1
    return migrated


# ─── M4-3 (#880): the durable unit→injected-bullet-ids map (the used-bullet correlation seam) ──────────
# Shared by the engine's handover injection site (WRITE — which bullets a unit's handover carried) and the
# M4-2 dev-process ledger scan (READ — populate Trajectory.used_bullet_ids so the Reflector rates which
# dev-loop bullets were helpful/harmful, E-004). A small JSON map under the install home, keyed by an opaque
# string (the engine task id AND any issue# the handover references — the standard `Closes #N` linkage), so a
# per-unit ledger trajectory (keyed by the issue#) finds the bullets injected across that unit's handovers.
# Stdlib-only, bounded, fail-soft; one source of truth for the path/format so both processes agree.
_DEVBULLETS_FILE = "ace_devbullets.json"
_DEVBULLETS_CAP = 1024            # bound the map (oldest keys dropped); a unit/handover count never approaches this


def _devbullets_path(home: "Path | str") -> "Optional[Path]":
    try:
        return Path(home) / _DEVBULLETS_FILE
    except Exception:   # noqa: BLE001 — a hostile home → no map
        return None


def record_unit_bullets(home: "Path | str", key: str, bullet_ids: "Sequence[str]") -> None:
    """Record (UNION) the *bullet_ids* injected under *key* (a task id or an issue#). Merges into the
    existing entry so a unit's multiple handovers accumulate. Bounded + atomic + fail-soft; a no-op on an
    empty key / empty ids."""
    k = (str(key) if key is not None else "").strip()
    ids = [str(b) for b in (bullet_ids or []) if str(b).strip()]
    if not (k and ids):
        return
    p = _devbullets_path(home)
    if p is None:
        return
    tmp = None
    try:
        try:
            data = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
        except Exception:   # noqa: BLE001 — corrupt map reads as empty (we rewrite below)
            data = {}
        if not isinstance(data, dict):
            data = {}
        prev = data.get(k) if isinstance(data.get(k), list) else []
        merged: List[str] = list(prev)
        for b in ids:                               # union, order-preserving
            if b not in merged:
                merged.append(b)
        data[k] = merged
        if len(data) > _DEVBULLETS_CAP:             # drop oldest (insertion-ordered dict) to stay bounded
            for stale in list(data)[: len(data) - _DEVBULLETS_CAP]:
                data.pop(stale, None)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.parent / f"{p.name}.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
    except Exception:   # noqa: BLE001 — advisory: a correlation write must never break the handover
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass


def read_unit_bullets(home: "Path | str", key: str) -> List[str]:
    """The bullet ids injected under *key* (a task id or an issue#), or ``[]``. Never raises."""
    k = (str(key) if key is not None else "").strip()
    p = _devbullets_path(home)
    if not k or p is None:
        return []
    try:
        if not p.is_file():
            return []
        data = json.loads(p.read_text(encoding="utf-8"))
        v = data.get(k) if isinstance(data, dict) else None
        return [str(b) for b in v] if isinstance(v, list) else []
    except Exception:   # noqa: BLE001 — advisory read
        return []


# ─── M5-3 (#884): the fork→proposal pointer (the MPR decision-matrix bound to a fork's unit) ──────────
# The MPR run itself lives under the active initiative (vault/<slug>/runs/<id>/synthesis.md, the B3 home).
# This is a small pointer (unit → the produced matrix text) under the install home so the dev-process ask
# surface can render it as a RECOMMENDATION at the fork, decoupled from the async worker that produced it.
# Boundary-clean (no GitHub literal); latest matrix per fork wins; bounded; fail-soft.
_FORKPROP_FILE = "ace_forkproposals.json"
_FORKPROP_CAP = 512
_FORKPROP_TEXT_CAP = 20000        # a decision-matrix synthesis is bounded; never store an unbounded blob


def _forkprop_path(home: "Path | str") -> "Optional[Path]":
    try:
        return Path(home) / _FORKPROP_FILE
    except Exception:   # noqa: BLE001
        return None


def record_fork_proposal(home: "Path | str", unit: str, proposal: str) -> None:
    """Record the MPR decision-matrix *proposal* produced for a fork *unit* (latest wins). Bounded + atomic +
    fail-soft; a no-op on an empty unit / empty proposal."""
    k = (str(unit) if unit is not None else "").strip()
    text = (str(proposal) if proposal is not None else "").strip()
    if not (k and text):
        return
    p = _forkprop_path(home)
    if p is None:
        return
    tmp = None
    try:
        try:
            data = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
        except Exception:   # noqa: BLE001 — corrupt map reads as empty (we rewrite below)
            data = {}
        if not isinstance(data, dict):
            data = {}
        data[k] = text[:_FORKPROP_TEXT_CAP]
        if len(data) > _FORKPROP_CAP:                 # drop oldest (insertion-ordered dict) to stay bounded
            for stale in list(data)[: len(data) - _FORKPROP_CAP]:
                data.pop(stale, None)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.parent / f"{p.name}.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
    except Exception:   # noqa: BLE001 — advisory: a proposal write must never break the fork worker
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass


def read_fork_proposal(home: "Path | str", unit: str) -> str:
    """The MPR decision-matrix produced for fork *unit*, or ``""`` (no proposal ⇒ the ask surfaces
    unchanged). Never raises."""
    k = (str(unit) if unit is not None else "").strip()
    p = _forkprop_path(home)
    if not k or p is None:
        return ""
    try:
        if not p.is_file():
            return ""
        data = json.loads(p.read_text(encoding="utf-8"))
        v = data.get(k) if isinstance(data, dict) else None
        return str(v) if isinstance(v, str) else ""
    except Exception:   # noqa: BLE001 — advisory read
        return ""


def list_fork_proposals(home: "Path | str") -> List[str]:
    """The units (issue#s) that currently have a recorded MPR fork proposal — so an operator surface can list
    the pending architecture proposals. Sorted; ``[]`` if none. Never raises."""
    p = _forkprop_path(home)
    if p is None:
        return []
    try:
        if not p.is_file():
            return []
        data = json.loads(p.read_text(encoding="utf-8"))
        return sorted(str(k) for k, v in data.items() if isinstance(v, str) and v) if isinstance(data, dict) else []
    except Exception:   # noqa: BLE001 — advisory read
        return []
