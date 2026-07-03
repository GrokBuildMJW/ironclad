#!/usr/bin/env python3
"""#1062: retention for the memory/state backups.

Keep the newest ``--keep`` timestamped backup directories under a backup root and prune the older ones. The
backup dirs are named with a UTC timestamp (``YYYYmmddTHHMMSSZ``), which sorts chronologically by name, so
selection is pure + deterministic. **Dry-run by default** — pass ``--apply`` to actually delete. stdlib-only;
the one piece of ``backup.sh`` with real logic, so it is unit-tested.

    python scripts/backup_retention.py ./ironclad-backups --keep 7            # report only
    python scripts/backup_retention.py ./ironclad-backups --keep 7 --apply    # prune old backups
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import List


def select_prunable(backup_dir, keep: int) -> "List[Path]":
    """The backup subdirs to prune — the oldest beyond the newest *keep* (by UTC-timestamp name). Returns []
    when *keep* <= 0 is not requested-as-purge... actually keep<=0 prunes ALL (an explicit purge). Pure."""
    d = Path(backup_dir)
    if not d.is_dir():
        return []
    subs = sorted((p for p in d.iterdir() if p.is_dir()), key=lambda p: p.name)
    if keep <= 0:
        return subs
    return subs[:-keep] if len(subs) > keep else []


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Prune old timestamped backups, keeping the newest N.")
    ap.add_argument("backup_dir", help="the backup root (contains timestamped backup dirs)")
    ap.add_argument("--keep", type=int, default=7, help="how many newest backups to keep (default 7)")
    ap.add_argument("--apply", action="store_true", help="actually delete (default: dry-run/report only)")
    a = ap.parse_args(argv)

    d = Path(a.backup_dir)
    if not d.is_dir():
        print(f"backup_retention: {d} not found (nothing to prune).")
        return 0
    prunable = select_prunable(d, a.keep)
    for p in prunable:
        print(("removed" if a.apply else "would remove") + f" old backup: {p.name}")
        if a.apply:
            shutil.rmtree(p, ignore_errors=True)
    if not prunable:
        print(f"backup_retention: nothing to prune (keep={a.keep}).")
    elif not a.apply:
        print("  (dry-run — re-run with --apply to delete)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
