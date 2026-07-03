"""#1064 (epic #1059): the deploy-side scheduler primitive — a jobs config + last-run state + a --run-due
pass, driven by ONE system cron entry, so periodic operate jobs (backup/prune/scans) run without the
orchestrator's single-shot execute_command. The due logic + state are pure/testable; the clock is injected."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SCH = Path(__file__).resolve().parents[2] / "scripts" / "scheduler.py"
pytestmark = pytest.mark.skipif(not _SCH.is_file(), reason="scripts/scheduler.py absent (installed tree)")
if _SCH.is_file():
    sys.path.insert(0, str(_SCH.parent))
    import scheduler as sch  # noqa: E402


def test_never_run_job_is_due():
    assert sch.due_jobs([{"name": "a", "command": "x", "interval_s": 100}], {}, now=1000.0)


def test_due_respects_the_interval():
    jobs = [{"name": "a", "command": "x", "interval_s": 100}]
    state = {"a": {"last_run": 950.0}}
    assert sch.due_jobs(jobs, state, now=1000.0) == []              # 50s < 100s → not due
    assert sch.due_jobs(jobs, state, now=1050.0)                    # exactly 100s → due


def test_run_due_runs_only_due_jobs_and_stamps():
    jobs = [{"name": "a", "command": "cmd-a", "interval_s": 100},
            {"name": "b", "command": "cmd-b", "interval_s": 100}]
    state = {"b": {"last_run": 990.0}}                              # b ran recently
    ran = []
    results = sch.run_due(jobs, state, now=1000.0, runner=lambda c: ran.append(c) or 0)
    assert ran == ["cmd-a"] and results == [{"name": "a", "rc": 0}]
    assert state["a"]["last_run"] == 1000.0


def test_failed_job_is_not_hot_retried():
    jobs = [{"name": "a", "command": "x", "interval_s": 100}]
    state = {}
    sch.run_due(jobs, state, now=1000.0, runner=lambda c: 1)        # fails
    assert state["a"]["last_rc"] == 1 and state["a"]["last_run"] == 1000.0
    assert sch.due_jobs(jobs, state, now=1050.0) == []             # within interval → NOT retried in a hot loop


def test_load_jobs_filters_malformed_and_handles_missing(tmp_path):
    cfg = tmp_path / "jobs.json"
    cfg.write_text(json.dumps({"jobs": [
        {"name": "ok", "command": "c", "interval_s": 60},
        {"name": "bad-no-cmd", "interval_s": 60},
        {"command": "c", "interval_s": 60},
    ]}), encoding="utf-8")
    assert [j["name"] for j in sch.load_jobs(cfg)] == ["ok"]
    assert sch.load_jobs(tmp_path / "nope.json") == []


def test_state_roundtrip(tmp_path):
    p = tmp_path / "sub" / "state.json"
    sch.save_state(p, {"a": {"last_run": 5.0}})
    assert sch.load_state(p) == {"a": {"last_run": 5.0}}


def test_main_run_due_end_to_end(tmp_path, monkeypatch):
    cfg = tmp_path / "jobs.json"
    cfg.write_text(json.dumps({"jobs": [{"name": "t", "command": "echo hi", "interval_s": 1}]}), encoding="utf-8")
    state = tmp_path / "state.json"
    monkeypatch.setattr(sch.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 0})())
    assert sch.main(["--config", str(cfg), "--state", str(state), "--run-due"]) == 0
    assert sch.load_state(state)["t"]["last_run"] > 0
