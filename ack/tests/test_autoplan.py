"""Continuation tick (#1296 — shared by the monolithic agent thread and the split server).

Verifies the post-advance decision logic without a model, in leg order: an open (handover-less,
eligible) unit → enqueue its [NEXT-UNIT] handover-authoring turn; no units + a configured
capability backlog → enqueue the [AUTOPLAN] planning turn; no source at all → idle but ARMED
(the tick never disables itself — only the max-tasks limit stops it). Work in flight
(in_progress, or pending WITH a staged handover) suppresses the tick entirely.
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
    def __init__(self, pending=(), in_progress=(), done=()):
        self._d = {"pending": list(pending), "in_progress": list(in_progress), "done": list(done)}

    def list(self, status=None):
        if status is None:
            return [t for rows in self._d.values() for t in rows]
        return list(self._d.get(status, []))


def _unit(tid, *, typ="implementation", prio="high", created="2026-07-08T10:00:00Z", **kw):
    return {"id": tid, "type": typ, "priority": prio, "title": f"unit {tid}",
            "description": "d", "created_at": created, **kw}


@pytest.fixture(autouse=True)
def _flags(monkeypatch, tmp_path):
    saved = (gx10.AUTOPILOT_AUTOPLAN, gx10.AUTOPILOT_MAX_TASKS, gx10._AUTOPLAN_DONE,
             gx10._EFFECTIVE_CFG)
    gx10.AUTOPILOT_AUTOPLAN = True
    gx10.AUTOPILOT_MAX_TASKS = 0
    gx10._AUTOPLAN_DONE = 0
    gx10._EFFECTIVE_CFG = {"paths": {"active_capability_backlog": "my-backlog.md"},
                           "autopilot": {}}
    monkeypatch.setattr(gx10, "_find_handover", lambda tid: None)
    monkeypatch.setattr(gx10, "archive_feedback_dir", lambda: tmp_path / "archive" / "feedback")
    yield
    (gx10.AUTOPILOT_AUTOPLAN, gx10.AUTOPILOT_MAX_TASKS, gx10._AUTOPLAN_DONE,
     gx10._EFFECTIVE_CFG) = saved


def _enqueue_capture():
    box = []
    return box, (lambda p: box.append(p))


# ── leg 1: open units ─────────────────────────────────────────────────────────

def test_open_unit_enqueues_next_unit_turn(monkeypatch):
    monkeypatch.setattr(gx10, "_store", lambda: _FakeStore(pending=[_unit("KGC-2")]))
    box, enq = _enqueue_capture()
    gx10._continuation_tick("KGC-1", enq)
    assert len(box) == 1
    assert box[0].startswith("[NEXT-UNIT]")
    assert "task_id='KGC-2'" in box[0] and "NO task_json" in box[0].replace("no task_json", "NO task_json")
    assert gx10._AUTOPLAN_DONE == 1


def test_next_unit_prompt_names_epic_progress(monkeypatch):
    epic = {"id": "KGC-1", "type": "epic", "priority": "high", "title": "the epic",
            "description": "d", "created_at": "2026-07-08T09:00:00Z"}
    kid = _unit("KGC-3", parent="KGC-1")
    done_kid = _unit("KGC-2", parent="KGC-1")
    done_kid["status"] = "done"
    monkeypatch.setattr(gx10, "_store",
                        lambda: _FakeStore(pending=[epic, kid], done=[done_kid]))
    prompt = gx10._next_unit_prompt("KGC-2", kid)
    assert "epic KGC-1: 1/2 units done" in prompt
    assert "PLAN-CHANGE DUTY" in prompt


def test_work_in_flight_suppresses_the_tick(monkeypatch):
    monkeypatch.setattr(gx10, "_store",
                        lambda: _FakeStore(in_progress=[_unit("KGC-2")], pending=[_unit("KGC-3")]))
    box, enq = _enqueue_capture()
    gx10._continuation_tick("KGC-1", enq)
    assert box == []                       # a coder is (about to be) running → not the planner's turn


def test_staged_pending_counts_as_in_flight(monkeypatch):
    monkeypatch.setattr(gx10, "_store", lambda: _FakeStore(pending=[_unit("KGC-2")]))
    monkeypatch.setattr(gx10, "_find_handover",
                        lambda tid: Path("h.md") if tid == "KGC-2" else None)
    box, enq = _enqueue_capture()
    gx10._continuation_tick("KGC-1", enq)
    assert box == []                       # staged → the LAUNCHER's job, nothing to plan


def test_deadlock_enqueues_nothing_but_stays_armed(monkeypatch):
    blocked = _unit("KGC-2", blocked=True)
    monkeypatch.setattr(gx10, "_store", lambda: _FakeStore(pending=[blocked]))
    box, enq = _enqueue_capture()
    gx10._continuation_tick("KGC-1", enq)
    assert box == []
    assert gx10.AUTOPILOT_AUTOPLAN is True  # surfaced, not disarmed


# ── leg 2: capability backlog ────────────────────────────────────────────────

def test_backlog_leg_after_units_drained(monkeypatch):
    monkeypatch.setattr(gx10, "_store", lambda: _FakeStore())
    box, enq = _enqueue_capture()
    gx10._continuation_tick("KGC-1", enq)
    assert len(box) == 1
    assert box[0].startswith("[AUTOPLAN]")
    assert "my-backlog.md" in box[0] and "stage_handover" in box[0]


def test_epic_records_do_not_block_the_backlog_leg(monkeypatch):
    epic = {"id": "KGC-1", "type": "epic", "priority": "high", "title": "e",
            "description": "d", "created_at": "2026-07-08T09:00:00Z"}
    monkeypatch.setattr(gx10, "_store", lambda: _FakeStore(pending=[epic]))
    box, enq = _enqueue_capture()
    gx10._continuation_tick("KGC-9", enq)
    assert len(box) == 1 and box[0].startswith("[AUTOPLAN]")


# ── leg 3: no source → idle, ARMED (#1296: no self-disable) ──────────────────

def test_no_source_stays_armed(monkeypatch):
    monkeypatch.setattr(gx10, "_store", lambda: _FakeStore())
    gx10._EFFECTIVE_CFG = {"paths": {}, "autopilot": {}}   # no backlog, no units
    box, enq = _enqueue_capture()
    gx10._continuation_tick("KGC-1", enq)
    assert box == []
    assert gx10.AUTOPILOT_AUTOPLAN is True  # the old self-disable is the #1296 root-cause — gone


# ── bounds & gating ──────────────────────────────────────────────────────────

def test_max_tasks_limit_stops(monkeypatch):
    monkeypatch.setattr(gx10, "_store", lambda: _FakeStore())
    gx10.AUTOPILOT_MAX_TASKS = 1
    box, enq = _enqueue_capture()
    gx10._continuation_tick("KGC-1", enq)      # 1/1 → limit reached
    assert box == [] and gx10.AUTOPILOT_AUTOPLAN is False


def test_off_is_noop(monkeypatch):
    monkeypatch.setattr(gx10, "_store", lambda: _FakeStore())
    gx10.AUTOPILOT_AUTOPLAN = False
    box, enq = _enqueue_capture()
    gx10._continuation_tick("KGC-1", enq)
    assert box == [] and gx10._AUTOPLAN_DONE == 0


# ── the pure selection policy ────────────────────────────────────────────────

def test_selection_priority_then_created_then_id(monkeypatch):
    monkeypatch.setattr(gx10, "_find_handover", lambda tid: None)
    units = [_unit("KGC-4", prio="normal", created="2026-07-08T09:00:00Z"),
             _unit("KGC-3", prio="high",   created="2026-07-08T11:00:00Z"),
             _unit("KGC-2", prio="high",   created="2026-07-08T10:00:00Z"),
             _unit("KGC-10", prio="high",  created="2026-07-08T10:00:00Z")]
    win, elig, n_open = gx10._select_next_unit(_FakeStore(pending=units))
    assert (win["id"], elig, n_open) == ("KGC-2", 4, 4)   # high beats normal; earlier beats later; 2 < 10


def test_selection_skips_blocked_and_unsatisfied_deps(monkeypatch):
    monkeypatch.setattr(gx10, "_find_handover", lambda tid: None)
    done = _unit("KGC-1"); done["status"] = "done"
    units = [_unit("KGC-2", blocked=True),
             _unit("KGC-3", dependencies=["KGC-999"]),     # unknown dep = unsatisfied (fail-closed)
             _unit("KGC-4", dependencies=["KGC-1"])]       # satisfied
    win, elig, n_open = gx10._select_next_unit(_FakeStore(pending=units, done=[done]))
    assert win["id"] == "KGC-4" and elig == 1 and n_open == 3


def test_selection_deadlock_reports_open_count(monkeypatch):
    monkeypatch.setattr(gx10, "_find_handover", lambda tid: None)
    units = [_unit("KGC-2", blocked=True), _unit("KGC-3", dependencies=["KGC-999"])]
    win, elig, n_open = gx10._select_next_unit(_FakeStore(pending=units))
    assert win is None and elig == 0 and n_open == 2


def test_selection_excludes_epics_and_staged(monkeypatch):
    epic = {"id": "KGC-1", "type": "epic", "priority": "critical", "title": "e",
            "description": "d", "created_at": "2026-07-08T08:00:00Z"}
    staged = _unit("KGC-2", prio="critical")
    open_u = _unit("KGC-3", prio="low")
    monkeypatch.setattr(gx10, "_find_handover",
                        lambda tid: Path("h.md") if tid == "KGC-2" else None)
    win, elig, n_open = gx10._select_next_unit(_FakeStore(pending=[epic, staged, open_u]))
    assert win["id"] == "KGC-3" and n_open == 1


# ── the bootstrap kick (#1296 — arming must stage the FIRST unit) ────────────

def test_kick_enqueues_bootstrap_turn(monkeypatch):
    monkeypatch.setattr(gx10, "_store", lambda: _FakeStore(pending=[_unit("KGC-2")]))
    gx10._continuation_kick()
    prompt = gx10._INPUT_QUEUE.get_nowait()
    assert prompt.startswith("[NEXT-UNIT] Automation armed")
    assert "task_id='KGC-2'" in prompt
    assert "PLAN-CHANGE" not in prompt          # no predecessor feedback on the bootstrap
    assert gx10._INPUT_QUEUE.empty()


def test_kick_is_noop_when_disarmed_busy_or_empty(monkeypatch):
    gx10.AUTOPILOT_AUTOPLAN = False
    monkeypatch.setattr(gx10, "_store", lambda: _FakeStore(pending=[_unit("KGC-2")]))
    assert gx10._continuation_kick() is False    # disarmed
    gx10.AUTOPILOT_AUTOPLAN = True
    monkeypatch.setattr(gx10, "_store", lambda: _FakeStore(in_progress=[_unit("KGC-3")]))
    assert gx10._continuation_kick() is False    # busy
    monkeypatch.setattr(gx10, "_store", lambda: _FakeStore())
    assert gx10._continuation_kick() is False    # nothing to select
    assert gx10._INPUT_QUEUE.empty()
