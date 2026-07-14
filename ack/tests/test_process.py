"""Process policy and the optional ACE-backed pre-turn hint (#1468 F7)."""
from __future__ import annotations

import sys
from pathlib import Path

from ack.process import (
    ProcessLessonKind,
    ProcessSignal,
    distill_process_lesson,
    format_process_hint,
)

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

SCOPE = "proj_pp::track::main"


def test_success_yields_working_path():
    lesson = distill_process_lesson(ProcessSignal(task_type="bugfix", succeeded=True, agent="claude"))
    assert lesson.kind is ProcessLessonKind.WORKING_PATH
    assert "bugfix" in lesson.text and "claude" in lesson.text


def test_success_includes_tool_sequence_and_retrieval():
    lesson = distill_process_lesson(ProcessSignal(
        task_type="feature", succeeded=True, tools=("read", "edit"), retrieval_hit=True))
    assert "read → edit" in lesson.text and "retrieval helped" in lesson.text


def test_missing_clarification_takes_priority():
    lesson = distill_process_lesson(ProcessSignal(
        task_type="feature", succeeded=True, missing_clarification="the target file path"))
    assert lesson.kind is ProcessLessonKind.MISSING_INPUT
    assert "the target file path" in lesson.text and "feature" in lesson.text


def test_unsuccessful_or_typeless_signal_yields_no_lesson():
    assert distill_process_lesson(ProcessSignal(task_type="bugfix", succeeded=False)) is None
    assert distill_process_lesson(ProcessSignal(task_type="", succeeded=True)) is None


def test_distill_never_raises_on_garbage():
    assert distill_process_lesson(None) is None
    assert distill_process_lesson("not-a-signal") is None
    lesson = distill_process_lesson(ProcessSignal(task_type="x", succeeded=True, tools=(5, None, "ok")))
    assert lesson.kind is ProcessLessonKind.WORKING_PATH


def test_format_hint_renders_and_respects_limit():
    out = format_process_hint(["a", "b", "c"], limit=2)
    assert out.startswith("Process notes") and out.count("\n- ") == 2


def test_format_hint_empty_or_garbage_is_blank():
    assert format_process_hint([]) == ""
    assert format_process_hint(None) == ""
    assert format_process_hint(["   ", 5]) == ""
    assert format_process_hint(object()) == ""


def _ace_hint_provider(tmp_path, monkeypatch):
    import gx10
    from ack import lessons
    from lesson_store import LessonCategory
    from playbook_store import PlaybookStore

    saved_provider, saved_cfg = lessons.get_provider(), gx10._EFFECTIVE_CFG
    store = PlaybookStore(tmp_path / "playbooks")
    store.record(SCOPE, "reuse the validated approach", LessonCategory.BEST_KNOWN_PATH)
    lessons.set_provider(store)
    monkeypatch.setattr(gx10, "_active_mem_ns", lambda default="": SCOPE, raising=False)
    return gx10, lessons, saved_provider, saved_cfg


def test_process_hint_is_default_off_even_with_always_on_ace_provider(tmp_path, monkeypatch):
    gx10, lessons, saved_provider, saved_cfg = _ace_hint_provider(tmp_path, monkeypatch)
    try:
        gx10._EFFECTIVE_CFG = gx10._code_defaults()
        assert gx10._process_hint() == ""
    finally:
        lessons.set_provider(saved_provider)
        gx10._EFFECTIVE_CFG = saved_cfg


def test_process_hint_reads_ace_provider_when_enabled(tmp_path, monkeypatch):
    gx10, lessons, saved_provider, saved_cfg = _ace_hint_provider(tmp_path, monkeypatch)
    try:
        cfg = gx10._code_defaults()
        cfg["process"]["hints_enabled"] = True
        gx10._EFFECTIVE_CFG = cfg
        assert "reuse the validated approach" in gx10._process_hint()
        cfg["process"]["max_hints"] = float("inf")
        assert "reuse the validated approach" in gx10._process_hint()
    finally:
        lessons.set_provider(saved_provider)
        gx10._EFFECTIVE_CFG = saved_cfg


def test_process_hint_is_blank_without_provider_or_scope(monkeypatch):
    import gx10
    from ack import lessons

    saved_provider, saved_cfg = lessons.get_provider(), gx10._EFFECTIVE_CFG
    try:
        lessons.set_provider(None)
        cfg = gx10._code_defaults()
        cfg["process"]["hints_enabled"] = True
        gx10._EFFECTIVE_CFG = cfg
        assert gx10._process_hint() == ""
        monkeypatch.setattr(gx10, "_active_mem_ns", lambda default="": "", raising=False)
        assert gx10._process_hint() == ""
    finally:
        lessons.set_provider(saved_provider)
        gx10._EFFECTIVE_CFG = saved_cfg


def test_legacy_process_alias_maps_warns_and_is_removed(capsys):
    import gx10

    cfg = gx10._code_defaults()
    cfg["process"]["enabled"] = True
    gx10._apply_config(cfg)
    assert "process.enabled" in capsys.readouterr().out
    assert "enabled" not in cfg["process"]
    assert cfg["process"]["hints_enabled"] is True


def test_runtime_legacy_process_alias_sets_canonical_key(monkeypatch):
    import gx10

    cfg = gx10._code_defaults()
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", cfg)
    surfaced = []
    monkeypatch.setattr(gx10, "_ui_print", lambda message, *a, **k: surfaced.append(str(message)))
    gx10._dispatch(None, "config set process.enabled true")
    assert cfg["process"]["hints_enabled"] is False
    assert gx10._EFFECTIVE_CFG is not cfg
    assert gx10._EFFECTIVE_CFG["process"]["hints_enabled"] is True
    assert "enabled" not in cfg["process"]
    assert len(surfaced) == 1 and "deprecated" in surfaced[0] and "process.hints_enabled" in surfaced[0]
