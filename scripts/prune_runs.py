#!/usr/bin/env python3
"""#1079: bounded retention for run artifacts.

An always-on deployment accumulates per-run artifacts under the workdir's ``runs/`` directories (MPR /
handover runs). This prunes entries older than ``--keep-days`` (default 30). **Dry-run by default** — pass
``--apply`` to actually delete. A deploy schedules it (a cron/systemd-timer today; the in-product scheduler
is #1064). Pure stdlib; never touches anything outside a ``runs/`` directory.

    python scripts/prune_runs.py /work --keep-days 30            # report only
    python scripts/prune_runs.py /work --keep-days 30 --apply    # delete old run artifacts
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path
from typing import List, Tuple


def _entry_size(entry: Path) -> int:
    """Total bytes of a file or a directory tree (best-effort; a vanished file counts as 0)."""
    try:
        if entry.is_file():
            return entry.stat().st_size
        return sum(f.stat().st_size for f in entry.rglob("*") if f.is_file())
    except OSError:
        return 0


def find_run_dirs(workdir: Path) -> List[Path]:
    """Every ``runs/`` directory under *workdir* (the MPR/handover run-artifact homes)."""
    return sorted(d for d in workdir.rglob("runs") if d.is_dir())


def prune_runs(workdir: Path, keep_days: float, apply: bool, now: float) -> Tuple[List[Tuple[Path, int]], int, int]:
    """Prune run-artifact entries older than *keep_days*. Returns (removed[(path,size)], kept_count,
    bytes_freed). With ``apply=False`` nothing is deleted — the list is what *would* be removed. *now* is
    injected so the caller (and tests) control the clock."""
    cutoff = now - keep_days * 86400.0
    removed: List[Tuple[Path, int]] = []
    kept = 0
    freed = 0
    for rd in find_run_dirs(workdir):
        for entry in sorted(rd.iterdir()):
            try:
                mtime = entry.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff:
                size = _entry_size(entry)
                removed.append((entry, size))
                freed += size
                if apply:
                    try:
                        if entry.is_dir():
                            shutil.rmtree(entry, ignore_errors=True)
                        else:
                            entry.unlink()
                    except OSError:
                        pass
            else:
                kept += 1
    return removed, kept, freed


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Prune old run artifacts under a workdir's runs/ directories.")
    ap.add_argument("workdir", help="the engine workdir (contains .ironclad/ + vault/<slug>/runs/)")
    ap.add_argument("--keep-days", type=float, default=30.0, help="delete run artifacts older than this (default 30)")
    ap.add_argument("--apply", action="store_true", help="actually delete (default: dry-run/report only)")
    args = ap.parse_args(argv)

    workdir = Path(args.workdir)
    if not workdir.is_dir():
        print(f"ERROR: workdir not found: {workdir}", file=sys.stderr)
        return 2

    removed, kept, freed = prune_runs(workdir, args.keep_days, args.apply, time.time())
    verb = "removed" if args.apply else "would remove"
    print(f"prune_runs: {verb} {len(removed)} artifact(s) older than {args.keep_days:g}d "
          f"({freed / 1e6:.1f} MB), kept {kept}.")
    for entry, size in removed:
        print(f"  {verb}: {entry}  ({size / 1e6:.2f} MB)")
    if not args.apply and removed:
        print("  (dry-run — re-run with --apply to delete)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
