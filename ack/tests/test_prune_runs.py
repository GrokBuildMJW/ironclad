"""#1079 (epic #1043 quick-win): bounded retention for run artifacts.

`prune_runs.py` purges entries older than keep-days from the workdir's `runs/` directories (MPR/handover
run artifacts) so an always-on deployment doesn't accumulate them forever. Dry-run by default; the clock is
injected for deterministic tests.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import prune_runs as pr  # noqa: E402


def _make(tmp_path):
    runs = tmp_path / "vault" / "proj" / "runs"
    runs.mkdir(parents=True)
    old = runs / "run-old"
    old.mkdir()
    (old / "a.md").write_text("x" * 100, encoding="utf-8")
    new = runs / "run-new.json"
    new.write_text("y", encoding="utf-8")
    now = time.time()
    old_t = now - 40 * 86400            # 40 days old → beyond a 30-day window
    os.utime(old, (old_t, old_t))
    os.utime(old / "a.md", (old_t, old_t))
    os.utime(new, (now, now))
    return old, new, now


def test_dry_run_reports_but_deletes_nothing(tmp_path):
    old, new, now = _make(tmp_path)
    removed, kept, freed = pr.prune_runs(tmp_path, keep_days=30, apply=False, now=now)
    assert [e for e, _ in removed] == [old]
    assert kept == 1 and freed > 0
    assert old.exists() and new.exists()          # dry-run mutates nothing


def test_apply_deletes_old_keeps_new(tmp_path):
    old, new, now = _make(tmp_path)
    removed, kept, freed = pr.prune_runs(tmp_path, keep_days=30, apply=True, now=now)
    assert not old.exists()
    assert new.exists()                           # within the window → kept
    assert kept == 1 and len(removed) == 1


def test_keep_days_window_is_respected(tmp_path):
    old, new, now = _make(tmp_path)
    # a 90-day window keeps the 40-day-old artifact
    removed, kept, _ = pr.prune_runs(tmp_path, keep_days=90, apply=True, now=now)
    assert removed == [] and kept == 2 and old.exists()


def test_finds_every_runs_dir(tmp_path):
    (tmp_path / "a" / "runs").mkdir(parents=True)
    (tmp_path / "b" / "c" / "runs").mkdir(parents=True)
    assert len(pr.find_run_dirs(tmp_path)) == 2


def test_main_missing_workdir_returns_2():
    assert pr.main(["/definitely/missing/workdir/xyz"]) == 2
