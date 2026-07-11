"""#1229 (S7) — disentangle /watcher vs /autopilot + a detect-progress heartbeat + an explicit BLOCKED state.

Three opt-in/default-off mechanisms (byte-identical when off):
  A automation.decoupled — autopilot is self-sufficient, the contradictory "watcher on required" message is gone
  B heartbeat.stall_seconds — an in_progress task with no log/feedback progress for N s is flagged stalled
  C blocked task-flag — mark_blocked/clear_blocked annotate a stuck task in place (no 4th directory state)
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=lambda **kw: object()))
_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402


def _setup(monkeypatch, tmp_path):
    gx10._apply_config(gx10._code_defaults())
    gx10.STORE = None
    monkeypatch.setattr(gx10, "_ui_print", lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)
    gx10.initiative_new("Demo", "software")


def _mk_inprogress(title="t"):
    tid = gx10._store().create(
        {"type": "feature", "priority": "high", "title": title, "description": "y"}, force=True)["id"]
    gx10._store().transition(tid, "in_progress")
    return tid


def _feedback(tid, status="done", agent="OPUS"):
    fb = gx10.feedback_dir() / f"{tid}_{agent}-feedback.md"
    fb.parent.mkdir(parents=True, exist_ok=True)
    fb.write_text(f"---\nstatus: {status}\n---\nx\n", encoding="utf-8")
    return fb


def _stale_log(tid, agent="OPUS", age=1000):
    logs = gx10.state_root() / gx10.AUTOPILOT_LOGS_DIR
    logs.mkdir(parents=True, exist_ok=True)
    p = logs / f"{tid}_{agent}.log"
    p.write_text("log\n", encoding="utf-8")
    old = os.stat(p).st_mtime - age
    os.utime(p, (old, old))
    return p


# ── Slice C: BLOCKED task-flag ────────────────────────────────────────────────
def test_mark_and_clear_blocked(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    tid = _mk_inprogress()
    gx10._store().mark_blocked(tid, reason="stuck", kind="blocked")
    t = gx10._store().get(tid)
    assert t["blocked"] is True and t["blocked_kind"] == "blocked" and t["blocked_reason"] == "stuck"
    assert t["status"] == "in_progress"                 # no folder move — still one of the 3 states
    gx10._store().clear_blocked(tid)
    assert "blocked" not in gx10._store().get(tid)


def test_transition_clears_blocked(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    tid = _mk_inprogress()
    gx10._store().mark_blocked(tid, reason="stuck")
    gx10._store().transition(tid, "done")               # advancing un-blocks
    assert "blocked" not in gx10._store().get(tid)


def test_board_shows_blocked(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    tid = _mk_inprogress("wedged")
    gx10._store().mark_blocked(tid, reason="no progress for 900s", kind="stalled")
    b = gx10._render_board(gx10.active_slug())
    assert "⚠ STALLED: no progress for 900s" in b


def test_board_byte_identical_without_blocked(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _mk_inprogress("plain")
    assert "⚠" not in gx10._render_board(gx10.active_slug())   # no blocked field → no marker


def test_advance_gate_marks_blocked(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(gx10, "ADVANCE_GATE_ENABLED", True)
    tid = _mk_inprogress()
    _feedback(tid, status="blocked")
    out = gx10._advance_pipeline(tid, "OPUS")
    assert "not advancing" in out
    t = gx10._store().get(tid)
    assert t.get("blocked") and t.get("blocked_kind") == "blocked" and t["status"] == "in_progress"


# ── Slice A: disentangle /watcher vs /autopilot ───────────────────────────────
def test_reconcile_decoupled_autopilot_only_skips_feedback(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(gx10, "AUTOMATION_DECOUPLED", True)
    monkeypatch.setattr(gx10, "_WATCHER_ENABLED", False)
    tid = _mk_inprogress()
    _feedback(tid, status="done")
    seen, enq, calls = {}, set(), []
    gx10._reconcile_once(gx10._store(), lambda *a: calls.append(a), seen, enq)
    gx10._reconcile_once(gx10._store(), lambda *a: calls.append(a), seen, enq)
    assert calls == []                                  # decoupled + watcher off → no feedback-advance


def test_reconcile_coupled_runs_feedback_byte_identical(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(gx10, "AUTOMATION_DECOUPLED", False)   # default → guard is a no-op
    monkeypatch.setattr(gx10, "_WATCHER_ENABLED", False)
    tid = _mk_inprogress()
    _feedback(tid, status="done")
    seen, enq, calls = {}, set(), []
    gx10._reconcile_once(gx10._store(), lambda *a: calls.append(a), seen, enq)
    gx10._reconcile_once(gx10._store(), lambda *a: calls.append(a), seen, enq)
    assert len(calls) == 1                              # coupled: feedback side runs (the caller gates the loop)


def test_autopilot_double_message_only_when_coupled(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(gx10, "_WATCHER_ENABLED", False)
    prints: list = []
    monkeypatch.setattr(gx10, "_ui_print", lambda *a, **k: prints.append(str(a[0]) if a else ""))
    monkeypatch.setattr(gx10, "AUTOMATION_DECOUPLED", True)
    gx10._dispatch(None, "autopilot on")
    assert "watcher on" not in " ".join(prints)         # decoupled → no contradictory second command
    prints.clear()
    monkeypatch.setattr(gx10, "AUTOMATION_DECOUPLED", False)
    gx10._dispatch(None, "autopilot on")
    text = " ".join(prints)
    assert "/auto on" in text                           # coupled → the hint points at the meta-switch
    assert "watcher on" not in text


# ── Slice B: detect-progress heartbeat ────────────────────────────────────────
def test_heartbeat_off_no_stall(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(gx10, "HEARTBEAT_STALL_S", 0.0)       # off (default)
    tid = _mk_inprogress()
    _stale_log(tid, age=1000)
    gx10._reconcile_once(gx10._store(), lambda *a: None, {}, set())
    assert "blocked" not in gx10._store().get(tid)


def test_heartbeat_marks_stalled(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(gx10, "HEARTBEAT_STALL_S", 100.0)
    tid = _mk_inprogress()
    _stale_log(tid, age=1000)
    enq: set = set()
    gx10._reconcile_once(gx10._store(), lambda *a: None, {}, enq)
    t = gx10._store().get(tid)
    assert t.get("blocked") and t.get("blocked_kind") == "stalled"
    assert f"__stall_{tid}" in enq                       # deduped (marked once)


def test_heartbeat_unstalls_on_progress(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(gx10, "HEARTBEAT_STALL_S", 100.0)
    tid = _mk_inprogress()
    logp = _stale_log(tid, age=1000)
    enq: set = set()
    gx10._reconcile_once(gx10._store(), lambda *a: None, {}, enq)   # marks stalled
    assert gx10._store().get(tid).get("blocked_kind") == "stalled"
    os.utime(logp, None)                                 # progress resumes (fresh log mtime)
    gx10._reconcile_once(gx10._store(), lambda *a: None, {}, enq)   # un-stalls
    assert "blocked" not in gx10._store().get(tid)
    assert f"__stall_{tid}" not in enq


def test_heartbeat_runs_when_decoupled_watcher_off(monkeypatch, tmp_path):
    # Sonnet finding 1: heartbeat is independent of the watcher/feedback concern — a wedged autopilot coder
    # must be flagged even in decoupled, watcher-off mode (the block sits BEFORE the feedback-side skip).
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(gx10, "HEARTBEAT_STALL_S", 100.0)
    monkeypatch.setattr(gx10, "AUTOMATION_DECOUPLED", True)
    monkeypatch.setattr(gx10, "_WATCHER_ENABLED", False)
    tid = _mk_inprogress()
    _stale_log(tid, age=1000)
    gx10._reconcile_once(gx10._store(), lambda *a: None, {}, set())
    assert gx10._store().get(tid).get("blocked_kind") == "stalled"


def test_heartbeat_does_not_clobber_gate_block(monkeypatch, tmp_path):
    # Sonnet finding 2: a task already blocked by the advance gate keeps its reason — heartbeat won't overwrite.
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(gx10, "HEARTBEAT_STALL_S", 100.0)
    tid = _mk_inprogress()
    gx10._store().mark_blocked(tid, reason="advance refused: status blocked", kind="blocked")
    _stale_log(tid, age=1000)
    gx10._reconcile_once(gx10._store(), lambda *a: None, {}, set())
    t = gx10._store().get(tid)
    assert t.get("blocked_kind") == "blocked"                       # NOT clobbered to 'stalled'
    assert "advance refused" in t.get("blocked_reason", "")


def test_task_progress_mtime_excludes_task_json(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    tid = _mk_inprogress()
    assert gx10._task_progress_mtime(gx10._store(), tid) is None   # no log/feedback → no false signal
    logp = _stale_log(tid, age=500)
    assert abs(gx10._task_progress_mtime(gx10._store(), tid) - os.stat(logp).st_mtime) < 0.01
