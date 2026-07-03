#!/usr/bin/env python3
"""#1064: deploy-side scheduler primitive for operate-phase periodic jobs.

The orchestrator's ``execute_command`` is single-shot (and its deny-list refuses scheduled-task/start-job),
so periodic operate jobs (backup, prune, scans, drift checks) need an external scheduler. This is a minimal,
testable one: a jobs config (name + shell command + interval), a last-run state file, and a ``--run-due``
pass that runs the jobs whose interval has elapsed. Drive it from ONE system cron entry (or a systemd timer)
that fires every minute — it fans out to the configured jobs, so you don't hand-maintain a cron line per job:

    * * * * *  cd /path/to/ironclad && python3 scripts/scheduler.py --run-due >> ./scheduler.log 2>&1

stdlib-only; the clock is injected (the caller passes ``now``) so the due logic is deterministic in tests.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, List


def load_jobs(config_path) -> "List[Dict[str, Any]]":
    """Read ``[{name, command, interval_s}]`` from a JSON file (a bare list or ``{"jobs": [...]}``). Missing
    file → []. Skips malformed entries (any missing field), never raises."""
    p = Path(config_path)
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:   # noqa: BLE001 — a corrupt config → no jobs, never crashes the tick
        return []
    raw = data.get("jobs", []) if isinstance(data, dict) else data
    out: "List[Dict[str, Any]]" = []
    for j in raw or []:
        if isinstance(j, dict) and j.get("name") and j.get("command") and j.get("interval_s"):
            try:
                out.append({"name": str(j["name"]), "command": str(j["command"]),
                            "interval_s": float(j["interval_s"])})
            except (TypeError, ValueError):
                continue
    return out


def load_state(state_path) -> "Dict[str, Any]":
    try:
        return json.loads(Path(state_path).read_text(encoding="utf-8"))
    except Exception:   # noqa: BLE001 — missing/corrupt → empty state (everything runs on the next tick)
        return {}


def save_state(state_path, state: "Dict[str, Any]") -> None:
    p = Path(state_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    tmp.replace(p)


def due_jobs(jobs: "List[Dict[str, Any]]", state: "Dict[str, Any]", now: float) -> "List[Dict[str, Any]]":
    """The jobs whose interval has elapsed since their last run (or which never ran). Pure."""
    due = []
    for j in jobs:
        last = float((state.get(j["name"]) or {}).get("last_run", 0.0) or 0.0)
        if now - last >= j["interval_s"]:
            due.append(j)
    return due


def run_due(jobs: "List[Dict[str, Any]]", state: "Dict[str, Any]", now: float,
            runner: "Callable[[str], int]") -> "List[Dict[str, Any]]":
    """Run every due job via ``runner(command) -> returncode`` and stamp ``last_run`` = *now* (whether or not
    it succeeded — a failed job is retried only after its interval, never in a hot loop). Mutates *state* in
    place; returns the run results."""
    results = []
    for j in due_jobs(jobs, state, now):
        rc = runner(j["command"])
        entry = state.setdefault(j["name"], {})
        entry["last_run"] = now
        entry["last_rc"] = rc
        results.append({"name": j["name"], "rc": rc})
    return results


def _shell_runner(command: str) -> int:
    try:
        return subprocess.run(command, shell=True).returncode
    except Exception:   # noqa: BLE001 — a spawn failure is a job failure, not a scheduler crash
        return -1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Run due operate-phase periodic jobs (drive every minute via cron).")
    ap.add_argument("--config", default="scripts/scheduler.jobs.json", help="jobs JSON (name/command/interval_s)")
    ap.add_argument("--state", default="./ironclad-workdir/.ironclad/scheduler-state.json", help="last-run state file")
    ap.add_argument("--run-due", action="store_true", help="run jobs whose interval elapsed (else just list)")
    a = ap.parse_args(argv)

    jobs = load_jobs(a.config)
    state = load_state(a.state)
    now = time.time()
    if not a.run_due:
        if not jobs:
            print("scheduler: no jobs configured.")
        for j in jobs:
            last = float((state.get(j["name"]) or {}).get("last_run", 0) or 0)
            due_in = 0 if not last else max(0.0, last + j["interval_s"] - now)
            print(f"{j['name']}: every {j['interval_s']:.0f}s · next due in {due_in:.0f}s")
        return 0

    results = run_due(jobs, state, now, _shell_runner)
    save_state(a.state, state)
    for r in results:
        print(f"ran {r['name']} → rc={r['rc']}")
    if not results:
        print("scheduler: nothing due.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
