"""Process-Level Self-Correction — the pure policy + the engine wiring (ACK, #602 S602-6).

Proves, offline:

  * `distill_process_lesson` maps a workflow signal to a typed lesson (missing-clarification first, else a
    success working-path; nothing actionable → None), and `format_process_hint` renders a compact block —
    both pure + never-raising;
  * the engine wiring is OPT-IN: `gx10._record_process_lesson` stores a TYPED process-lesson via the concrete
    EngineLessonStore only when `process.enabled` + a concrete provider is registered, and `_process_hint`
    surfaces it pre-turn — both byte-identical no-ops by default; never break a turn.

    python -m pytest ack/tests/test_process.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from ack.process import (
    ProcessLesson,
    ProcessLessonKind,
    ProcessSignal,
    distill_process_lesson,
    format_process_hint,
)

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

SCOPE = "proj_pp::track::main"


# ─── pure policy: distill ───────────────────────────────────────────────────────────────────────────
def test_success_yields_working_path():
    l = distill_process_lesson(ProcessSignal(task_type="bugfix", succeeded=True, agent="claude"))
    assert l.kind is ProcessLessonKind.WORKING_PATH
    assert "bugfix" in l.text and "claude" in l.text


def test_success_includes_tool_sequence_and_retrieval():
    l = distill_process_lesson(ProcessSignal(
        task_type="feature", succeeded=True, tools=("read", "edit"), retrieval_hit=True))
    assert "read → edit" in l.text and "retrieval helped" in l.text


def test_missing_clarification_takes_priority():
    l = distill_process_lesson(ProcessSignal(
        task_type="feature", succeeded=True, missing_clarification="the target file path"))
    assert l.kind is ProcessLessonKind.MISSING_INPUT
    assert "the target file path" in l.text and "feature" in l.text


def test_unsuccessful_without_clarification_is_none():
    assert distill_process_lesson(ProcessSignal(task_type="bugfix", succeeded=False)) is None


def test_success_without_type_is_none():
    assert distill_process_lesson(ProcessSignal(task_type="", succeeded=True)) is None


def test_distill_never_raises_on_garbage():
    assert distill_process_lesson(None) is None
    assert distill_process_lesson("not-a-signal") is None
    assert distill_process_lesson(ProcessSignal(task_type="x", succeeded=True, tools=(5, None, "ok"))).kind \
        is ProcessLessonKind.WORKING_PATH   # non-str tools dropped, no raise


# ─── pure policy: format hint ────────────────────────────────────────────────────────────────────────
def test_format_hint_renders_items():
    out = format_process_hint(["approach A", "approach B"])
    assert "approach A" in out and "approach B" in out and out.startswith("Process notes")


def test_format_hint_empty_is_blank():
    assert format_process_hint([]) == ""
    assert format_process_hint(None) == ""
    assert format_process_hint(["   ", 5]) == ""        # only non-empty strings count


def test_format_hint_respects_limit():
    out = format_process_hint(["a", "b", "c", "d"], limit=2)
    assert out.count("\n- ") == 2


def test_format_hint_never_raises():
    assert format_process_hint(object()) == ""          # non-iterable → blank, no raise


# ─── engine wiring — OPT-IN, concrete-provider-only, byte-identical default ─────────────────────────
@pytest.fixture
def _env(monkeypatch):
    """Isolate ack.lessons provider + gx10._EFFECTIVE_CFG; bind a fixed active scope (no real project needed
    — the base/no-project scope is "" and the store skips an empty scope, which is the byte-identical base
    behaviour). Resets after."""
    import gx10
    from ack import lessons as L
    saved_prov, saved_cfg = L.get_provider(), gx10._EFFECTIVE_CFG
    L.set_provider(None)
    monkeypatch.setattr(gx10, "_active_mem_ns", lambda default="": SCOPE, raising=False)
    yield gx10, L
    L.set_provider(saved_prov)
    gx10._EFFECTIVE_CFG = saved_cfg


def _enable(gx10, tmp_path):
    """Register a real EngineLessonStore + turn process.enabled on."""
    from lesson_store import EngineLessonStore
    from ack import lessons as L
    L.set_provider(EngineLessonStore(tmp_path / "lessons"))
    cfg = gx10._code_defaults()
    cfg["process"]["enabled"] = True
    gx10._EFFECTIVE_CFG = cfg


def test_record_and_hint_roundtrip_when_enabled(tmp_path, _env):
    gx10, L = _env
    _enable(gx10, tmp_path)
    gx10._record_process_lesson({"type": "bugfix"}, "claude")
    hint = gx10._process_hint()
    assert "bugfix" in hint and hint.startswith("Process notes")


def test_record_is_noop_when_disabled(tmp_path, _env):
    gx10, L = _env
    from lesson_store import EngineLessonStore, LessonCategory
    store = EngineLessonStore(tmp_path / "lessons")
    L.set_provider(store)
    gx10._EFFECTIVE_CFG = gx10._code_defaults()          # process.enabled is False by default
    gx10._record_process_lesson({"type": "bugfix"}, "claude")
    # inspect the store DIRECTLY (not via the disabled hint) — nothing was recorded.
    assert store.by_category(SCOPE, LessonCategory.BEST_KNOWN_PATH) == []


def test_record_is_noop_without_concrete_provider(tmp_path, _env):
    gx10, L = _env
    cfg = gx10._code_defaults(); cfg["process"]["enabled"] = True
    gx10._EFFECTIVE_CFG = cfg
    # no provider registered → concrete-provider gate closes → no-op, no raise.
    gx10._record_process_lesson({"type": "bugfix"}, "claude")
    assert gx10._process_hint() == ""


def test_record_ignores_string_only_provider(tmp_path, _env):
    """A non-EngineLessonStore (string-only) provider must be ignored — typed record/by_category need the
    concrete class (C0 fork-2)."""
    gx10, L = _env

    calls = []

    class _StringOnly:   # the 3-verb string seam — NONE of its methods may be touched by process-SC
        def get_lessons(self, scope, query="", limit=10): calls.append("get"); return []
        def report_lesson(self, scope, lesson, metadata=None): calls.append("report")
        def brief(self, scopes, limit=10): calls.append("brief"); return ""

    L.set_provider(_StringOnly())
    cfg = gx10._code_defaults(); cfg["process"]["enabled"] = True
    gx10._EFFECTIVE_CFG = cfg
    gx10._record_process_lesson({"type": "bugfix"}, "claude")   # ignored — not a concrete provider
    assert gx10._process_hint() == ""
    assert calls == []                                          # the forbidden string seam was never touched


def test_record_never_raises_on_garbage_task(tmp_path, _env):
    gx10, L = _env
    _enable(gx10, tmp_path)
    gx10._record_process_lesson(None, "")                # garbage existing → no raise, nothing actionable
    gx10._record_process_lesson("not-a-dict", "")
    assert gx10._process_hint() == ""                    # success+no-type → None → nothing stored


def test_process_hint_overflow_max_hints_never_raises(tmp_path, _env):
    """A non-finite process.max_hints (int(inf) raises OverflowError) falls back to the default, not "" /
    a raise."""
    gx10, L = _env
    _enable(gx10, tmp_path)
    gx10._record_process_lesson({"type": "bugfix"}, "claude")
    gx10._EFFECTIVE_CFG["process"]["max_hints"] = float("inf")
    hint = gx10._process_hint()
    assert "bugfix" in hint                       # inf max_hints → default limit, the hint still renders


def test_empty_scope_is_noop(tmp_path, _env, monkeypatch):
    """No project bound (base partition, mem_ns "") → record + hint must NOT touch the store at all."""
    gx10, L = _env
    from lesson_store import EngineLessonStore

    class _SpyStore(EngineLessonStore):
        def __init__(self, base):
            super().__init__(base)
            self.calls = []
        def record(self, *a, **k): self.calls.append("record"); return super().record(*a, **k)
        def by_category(self, *a, **k): self.calls.append("by_category"); return super().by_category(*a, **k)

    spy = _SpyStore(tmp_path / "lessons")
    L.set_provider(spy)
    cfg = gx10._code_defaults(); cfg["process"]["enabled"] = True
    gx10._EFFECTIVE_CFG = cfg
    monkeypatch.setattr(gx10, "_active_mem_ns", lambda default="": "", raising=False)
    gx10._record_process_lesson({"type": "bugfix"}, "claude")   # empty scope → must return before record
    assert gx10._process_hint() == ""                            # empty scope → must return before by_category
    assert spy.calls == []                                       # the store was never touched
