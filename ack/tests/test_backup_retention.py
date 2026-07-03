"""#1062 (epic #1059): backup retention — keep the newest N timestamped memory/state backups, prune older.

The one piece of scripts/backup.sh with real logic (the tar/docker glue is exercised by the deploy, not
unit-tested). UTC-timestamp directory names sort chronologically, so selection is pure + deterministic.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# The helper lives in the exported core/scripts tree; guard for its absence so the test SKIPS (not crashes)
# on an installed/clean-room tree, per the export-test-drift idiom (#845).
_BR = Path(__file__).resolve().parents[2] / "scripts" / "backup_retention.py"
pytestmark = pytest.mark.skipif(not _BR.is_file(),
                                reason="scripts/backup_retention.py absent (installed tree)")
if _BR.is_file():
    sys.path.insert(0, str(_BR.parent))
    import backup_retention as br  # noqa: E402


def _mk(tmp_path, names):
    for n in names:
        (tmp_path / n).mkdir()
    return tmp_path


def test_keeps_newest_and_selects_older_to_prune(tmp_path):
    _mk(tmp_path, ["20260101T000000Z", "20260102T000000Z", "20260103T000000Z", "20260104T000000Z"])
    prunable = br.select_prunable(tmp_path, keep=2)
    assert [p.name for p in prunable] == ["20260101T000000Z", "20260102T000000Z"]


def test_nothing_prunable_within_keep(tmp_path):
    _mk(tmp_path, ["20260101T000000Z", "20260102T000000Z"])
    assert br.select_prunable(tmp_path, keep=5) == []


def test_keep_zero_selects_all_for_purge(tmp_path):
    _mk(tmp_path, ["a", "b", "c"])
    assert len(br.select_prunable(tmp_path, keep=0)) == 3


def test_main_apply_deletes_old_keeps_newest(tmp_path):
    _mk(tmp_path, ["20260101T000000Z", "20260102T000000Z", "20260103T000000Z"])
    assert br.main([str(tmp_path), "--keep", "1", "--apply"]) == 0
    assert sorted(p.name for p in tmp_path.iterdir()) == ["20260103T000000Z"]


def test_main_dry_run_deletes_nothing(tmp_path):
    _mk(tmp_path, ["20260101T000000Z", "20260102T000000Z"])
    br.main([str(tmp_path), "--keep", "1"])                 # no --apply
    assert len(list(tmp_path.iterdir())) == 2


def test_main_missing_dir_is_a_noop(tmp_path):
    assert br.main([str(tmp_path / "does-not-exist"), "--keep", "3"]) == 0
