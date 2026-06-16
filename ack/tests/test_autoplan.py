"""Autoplan tick (shared by the monolithic agent thread and the split server).

Verifies the decision logic without a model: empty pipeline → enqueue a planning turn
built from the configured backlog; max-tasks limit stops it; a non-empty pipeline or a
missing backlog enqueues nothing.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=object))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402
import pytest  # noqa: E402


class _FakeStore:
    def __init__(self, pending=(), in_progress=()):
        self._p, self._ip = list(pending), list(in_progress)

    def list(self, status=None):
        return {"pending": self._p, "in_progress": self._ip}.get(status, [])


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    saved = (gx10.AUTOPILOT_AUTOPLAN, gx10.AUTOPILOT_MAX_TASKS, gx10._AUTOPLAN_DONE,
             gx10._EFFECTIVE_CFG)
    gx10.AUTOPILOT_AUTOPLAN = True
    gx10.AUTOPILOT_MAX_TASKS = 0
    gx10._AUTOPLAN_DONE = 0
    gx10._EFFECTIVE_CFG = {"paths": {"active_capability_backlog": "my-backlog.md"},
                           "autopilot": {}}
    yield
    (gx10.AUTOPILOT_AUTOPLAN, gx10.AUTOPILOT_MAX_TASKS, gx10._AUTOPLAN_DONE,
     gx10._EFFECTIVE_CFG) = saved


def _enqueue_capture():
    box = []
    return box, (lambda p: box.append(p))


def test_empty_pipeline_enqueues_plan(monkeypatch):
    monkeypatch.setattr(gx10, "_store", lambda: _FakeStore())
    box, enq = _enqueue_capture()
    gx10._autoplan_tick("KGC-1", enq)
    assert len(box) == 1
    assert "my-backlog.md" in box[0] and "stage_handover" in box[0]
    assert gx10._AUTOPLAN_DONE == 1


def test_non_empty_pipeline_does_not_enqueue(monkeypatch):
    monkeypatch.setattr(gx10, "_store", lambda: _FakeStore(pending=["KGC-2"]))
    box, enq = _enqueue_capture()
    gx10._autoplan_tick("KGC-1", enq)
    assert box == []                       # noch Arbeit offen → nichts planen


def test_max_tasks_limit_stops(monkeypatch):
    monkeypatch.setattr(gx10, "_store", lambda: _FakeStore())
    gx10.AUTOPILOT_MAX_TASKS = 1
    box, enq = _enqueue_capture()
    gx10._autoplan_tick("KGC-1", enq)      # 1/1 → Limit erreicht
    assert box == [] and gx10.AUTOPILOT_AUTOPLAN is False


def test_no_backlog_disables(monkeypatch):
    monkeypatch.setattr(gx10, "_store", lambda: _FakeStore())
    gx10._EFFECTIVE_CFG = {"paths": {}, "autopilot": {}}   # kein Backlog
    box, enq = _enqueue_capture()
    gx10._autoplan_tick("KGC-1", enq)
    assert box == [] and gx10.AUTOPILOT_AUTOPLAN is False


def test_off_is_noop(monkeypatch):
    monkeypatch.setattr(gx10, "_store", lambda: _FakeStore())
    gx10.AUTOPILOT_AUTOPLAN = False
    box, enq = _enqueue_capture()
    gx10._autoplan_tick("KGC-1", enq)
    assert box == [] and gx10._AUTOPLAN_DONE == 0
