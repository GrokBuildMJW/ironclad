"""#602 2.2 / #803 — Process-SC RE-HOMED onto the `post_feedback` Hook-Bus event.

Proves the Process-Level Self-Correction write is now driven by a bus consumer (`gx10._process_consumer_hook`)
through the real `_advance_pipeline` WRAPPER — not the old inline call in `_advance_pipeline_impl`:

  * with `process.enabled` (+ `lessons.enabled` so a concrete EngineLessonStore is wired) a completed advance
    records a typed process-lesson and the consumer is registered on `post_feedback`;
  * default OFF → no consumer registered, no record (byte-identical no-op);
  * the consumer gates on a FRESH completion → an already-done re-advance does NOT double-record;
  * a raising provider never breaks the advance (fail-soft).

    python -m pytest ack/tests/test_process_rehome_wiring.py -q
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=lambda **kw: object()))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10
from ack import hooks
from ack import lessons as L
from lesson_store import EngineLessonStore, LessonCategory


class _FakeAgent:
    def run(self, t): pass
    def save_session(self): pass
    def status(self): return "ok"


_TASK = '{"type":"feature","priority":"high","title":"Build X","description":"do it"}'


@pytest.fixture(autouse=True)
def _reset():
    hooks.clear_hooks()
    L.set_provider(None)
    saved = gx10._EFFECTIVE_CFG
    yield
    hooks.clear_hooks()
    L.set_provider(None)
    gx10._EFFECTIVE_CFG = saved
    gx10._apply_config(gx10._code_defaults())


def _enable(cfg_mut, tmp_path):
    """A cfg with process+lessons on, a concrete store wired (kept by _apply_lessons_provider), and the
    effective cfg set so _record_process_lesson's gate sees process.enabled."""
    store = EngineLessonStore(tmp_path / "lessons")
    L.set_provider(store)                       # concrete provider; lessons.enabled keeps it (not clobbered)
    cfg = gx10._code_defaults()
    cfg["lessons"]["enabled"] = True
    cfg["process"]["enabled"] = True
    cfg_mut(cfg)
    gx10._EFFECTIVE_CFG = cfg
    return cfg, store


def _drive_to_completion(tmp_path, monkeypatch, cfg):
    gx10.STORE = None
    monkeypatch.chdir(tmp_path)
    # bind a fixed non-empty scope (like test_process.py) — _dispatch(initiative new) does not bind the
    # thread ProjectContext, and Process-SC no-ops on the empty base scope.
    monkeypatch.setattr(gx10, "_active_mem_ns", lambda default="": "proj_rehome::track::main", raising=False)
    gx10._apply_config(cfg)                     # registers the post_feedback Process-SC consumer
    gx10._dispatch(_FakeAgent(), "initiative new Order Service --type software")
    gx10._stage_handover(None, "OPUS", "## Handover\nbuild it", task_json=_TASK, force=True)
    tid = gx10._store().list("pending")[0]["id"]
    (gx10.feedback_dir() / f"{tid}_OPUS-feedback.md").write_text("done", encoding="utf-8")
    out = gx10._advance_pipeline(tid, "OPUS")
    return tid, out


def test_process_consumer_records_via_advance_wrapper(tmp_path, monkeypatch):
    cfg, _store = _enable(lambda c: None, tmp_path)
    _tid, out = _drive_to_completion(tmp_path, monkeypatch, cfg)
    assert out.startswith("OK: pipeline advanced")
    assert "post_feedback" in hooks.registered_events()     # the consumer is wired on the bus
    assert "feature" in gx10._process_hint()                # the re-homed consumer recorded a typed lesson


def test_disabled_is_noop_no_consumer(tmp_path, monkeypatch):
    # process OFF → the Process-SC consumer records nothing (byte-identical). (`lessons.enabled` is on only to
    # keep the concrete provider so this isolates `process.enabled`; since #804 the Lessons consumer also rides
    # `post_feedback` on provider presence, so we assert the PROCESS effect behaviorally — no process-lesson
    # recorded — not bus-event membership.)
    store = EngineLessonStore(tmp_path / "lessons")
    L.set_provider(store)
    cfg = gx10._code_defaults()
    cfg["lessons"]["enabled"] = True            # keep the concrete provider so the gate isolates process.enabled
    gx10._EFFECTIVE_CFG = cfg
    _tid, out = _drive_to_completion(tmp_path, monkeypatch, cfg)
    assert out.startswith("OK: pipeline advanced")
    # read the store DIRECTLY (the _process_hint read-gate also honors process.enabled, so it would short-
    # circuit to "" even if a write had erroneously happened) — this isolates the WRITE side.
    assert store.by_category("proj_rehome::track::main", LessonCategory.BEST_KNOWN_PATH) == []
    assert gx10._process_hint() == ""           # and no hint surfaces either


def test_already_done_readvance_does_not_double_record(tmp_path, monkeypatch):
    calls = []

    class _SpyStore(EngineLessonStore):
        def record(self, *a, **k):
            calls.append(1)
            return super().record(*a, **k)

    cfg = gx10._code_defaults()
    cfg["lessons"]["enabled"] = True
    cfg["process"]["enabled"] = True
    gx10._EFFECTIVE_CFG = cfg
    spy = _SpyStore(tmp_path / "lessons")
    L.set_provider(spy)
    tid, out = _drive_to_completion(tmp_path, monkeypatch, cfg)
    assert out.startswith("OK: pipeline advanced")
    assert len(calls) == 1
    again = gx10._advance_pipeline(tid, "OPUS")                # already done → "OK: task ... already done"
    assert "already done" in again
    assert len(calls) == 1                                     # gate blocked the re-advance: NO second record


def test_consumer_failsoft_on_raising_provider(tmp_path, monkeypatch):
    class _Raising(EngineLessonStore):
        def record(self, *a, **k):
            raise RuntimeError("boom")

    cfg = gx10._code_defaults()
    cfg["lessons"]["enabled"] = True
    cfg["process"]["enabled"] = True
    gx10._EFFECTIVE_CFG = cfg
    L.set_provider(_Raising(tmp_path / "lessons"))
    _tid, out = _drive_to_completion(tmp_path, monkeypatch, cfg)
    assert out.startswith("OK: pipeline advanced")             # a raising provider never breaks the advance
