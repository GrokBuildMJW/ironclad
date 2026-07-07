"""#1226 (S4) — the model-invokable trigger verb ``launch_coder``.

The orchestrator (the single steering author) launches the coder for a staged handover ON DEMAND — bypassing
the autopilot daemon (which stays off by default; ADR-0002 D7 / #312 S4: no second steering authority). These
tests drive the verb THROUGH the tool dispatcher and assert: it launches + flips the task to in_progress
WITHOUT ``AUTOPILOT_ENABLED``; it is a clear no-op when nothing is staged; it respects the concurrency cap and
the double-launch guard; it honours an explicit ``task_id``; and it fails closed on an unknown/absent agent.
Modelled on ``test_code_agent_registry.py`` (the ``_do_launch`` Popen-fake pattern).
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=lambda **kw: object()))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402

_TASK = {"type": "feature", "priority": "high", "title": "wire", "description": "x"}


class _FakeProc:
    pid = 1

    def poll(self):
        return 0

    def wait(self, *a, **k):
        return 0


def _setup(monkeypatch, tmp_path, *, agent="OPUS", stage=True, frontmatter="---\n---\nho"):
    """Fresh engine state + an active initiative + (optionally) a pending task with a staged handover.
    Returns (tid, popen_calls); popen_calls records every ``Popen(argv)`` so a test can assert launch/no-launch."""
    gx10._apply_config(gx10._code_defaults())
    gx10.STORE = None
    gx10._AUTOPILOT_ACTIVE = 0            # isolate from any prior test's slot accounting
    gx10._AUTOPILOT_PROCS.clear()
    monkeypatch.setattr(gx10, "_ui_print", lambda *a, **k: None)  # cp1252-safe (the _wait daemon prints)
    monkeypatch.chdir(tmp_path)
    gx10.initiative_new("Auto", "software")
    tid = gx10._store().create(dict(_TASK), force=True)["id"]
    if stage:
        (gx10.handovers_dir() / f"{tid}_{agent}.md").write_text(frontmatter, encoding="utf-8")
    popen_calls = []

    def _fake_popen(a, *args, **kw):
        popen_calls.append(list(a))
        return _FakeProc()

    monkeypatch.setattr(gx10.subprocess, "Popen", _fake_popen)
    return tid, popen_calls


def _launch(task_id=None):
    return gx10._run_tool_dispatch("launch_coder", {"task_id": task_id} if task_id else {})


def test_launch_coder_launches_staged_and_flips_in_progress(monkeypatch, tmp_path):
    tid, popen = _setup(monkeypatch, tmp_path)
    assert gx10.AUTOPILOT_ENABLED is False              # the verb must NOT require autopilot on
    out = _launch()
    assert out.startswith("OK:")
    assert len(popen) == 1                              # the coder was actually spawned
    assert gx10._store().get(tid)["status"] == "in_progress"


def test_launch_coder_noop_when_nothing_staged(monkeypatch, tmp_path):
    _tid, popen = _setup(monkeypatch, tmp_path, stage=False)   # a task exists but has NO handover
    out = _launch()
    assert "No staged handover" in out
    assert popen == []


def test_launch_coder_respects_max_concurrent(monkeypatch, tmp_path):
    tid, popen = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(gx10, "AUTOPILOT_MAX_CONCURRENT", 1, raising=False)
    monkeypatch.setattr(gx10, "_autopilot_active", lambda: 1)  # one coder already running (deterministic)
    out = _launch()
    assert out.startswith("BUSY:")
    assert popen == []
    assert gx10._store().get(tid)["status"] == "pending"


def test_launch_coder_double_launch_guard(monkeypatch, tmp_path):
    tid, popen = _setup(monkeypatch, tmp_path)
    gx10._store().transition(tid, "in_progress")        # already running
    # the DEFAULT resolver scans only `pending`, so it naturally skips an in_progress task (no double-launch);
    # the explicit-id path still hits the guard with a clear message.
    out = _launch(task_id=tid)
    assert "already in_progress" in out
    assert popen == []


def test_launch_coder_default_skips_in_progress(monkeypatch, tmp_path):
    tid, popen = _setup(monkeypatch, tmp_path)
    gx10._store().transition(tid, "in_progress")        # the only unit is already running
    out = _launch()                                     # default resolution finds nothing new to launch
    assert "No staged handover" in out
    assert popen == []


def test_launch_coder_explicit_task_id(monkeypatch, tmp_path):
    tid, popen = _setup(monkeypatch, tmp_path)
    out = _launch(task_id=tid)
    assert out.startswith("OK:")
    assert len(popen) == 1
    assert gx10._store().get(tid)["status"] == "in_progress"


def test_launch_coder_unknown_task_id(monkeypatch, tmp_path):
    _tid, popen = _setup(monkeypatch, tmp_path)
    out = _launch(task_id="KGC-999")
    assert out.startswith("ERROR: no such task")
    assert popen == []


def test_launch_coder_unknown_agent_fails_closed(monkeypatch, tmp_path):
    tid, popen = _setup(monkeypatch, tmp_path, agent="ZZZ")   # ZZZ is not in the default OPUS/SONNET registry
    out = _launch()
    assert out.startswith("ERROR: unknown/unconfigured agent")
    assert popen == []
    assert gx10._store().get(tid)["status"] == "pending"


def test_launch_coder_server_topology_message_when_no_agents(monkeypatch, tmp_path):
    tid, popen = _setup(monkeypatch, tmp_path)
    # server topology = providers disabled → the registry is empty; the coder runs on the CLIENT, not here.
    class _EmptyReg:
        def has(self, name):
            return False

        def names(self):
            return []

    monkeypatch.setattr(gx10, "_code_agent_registry", lambda: _EmptyReg())
    monkeypatch.setattr(gx10, "_agent_names", lambda: [])
    out = _launch()
    assert "server topology" in out
    assert popen == []
    assert gx10._store().get(tid)["status"] == "pending"


def test_launch_coder_releases_slot_when_do_launch_raises(monkeypatch, tmp_path):
    # Finding #1: _do_launch can raise BEFORE its own release paths (a bad logdir, a non-KeyError transition
    # error). _trigger_coder must catch it and free the reserved slot — else every later launch reports BUSY
    # forever (the counter is permanently short one slot).
    _tid, popen = _setup(monkeypatch, tmp_path)

    def _boom(*a, **k):
        raise OSError("logdir unwritable")

    monkeypatch.setattr(gx10, "_do_launch", _boom)
    before = gx10._autopilot_active()
    out = _launch()
    assert out.startswith("ERROR")
    assert "slot released" in out
    assert gx10._autopilot_active() == before          # no slot leak


def test_launch_coder_error_when_spawn_fails(monkeypatch, tmp_path):
    # A Popen failure inside _do_launch's guarded block: it releases the slot + leaves the task pending; the
    # verb must report ERROR (not a false OK) and not flip the status.
    tid, _popen = _setup(monkeypatch, tmp_path)

    def _fail_popen(*a, **k):
        raise OSError("no agent binary")

    monkeypatch.setattr(gx10.subprocess, "Popen", _fail_popen)
    before = gx10._autopilot_active()
    out = _launch()
    assert out.startswith("ERROR")
    assert gx10._store().get(tid)["status"] == "pending"   # not flipped on a failed spawn
    assert gx10._autopilot_active() == before              # slot released
