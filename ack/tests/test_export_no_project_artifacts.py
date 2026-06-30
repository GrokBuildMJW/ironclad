"""Export invariant (#601 S17 / AD-8): the published export carries NO runtime project state.

Runtime project-isolation artifacts — the hidden per-project engine machinery (`.ironclad/`), per-track
vault subtrees (`.tracks/`), and the installation-global project registry (`registry.json`) — are created
under a project root at runtime and must never reach the export. `export_core.scan_project_artifacts`
asserts their absence (a backstop to `_COPY_IGNORE`).

The guard lives in `scripts/ci/` (private, not exported), so this test **skips** on an installed/clean-room
tree where `scripts/ci/` is absent (mirrors `test_export_leak_guard.py`).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_EXPORT = _REPO / "scripts" / "ci" / "export_core.py"

pytestmark = pytest.mark.skipif(
    not _EXPORT.is_file(),
    reason="private CI guard (scripts/ci/export_core.py) absent — installed/clean-room tree",
)


def _load():
    spec = importlib.util.spec_from_file_location("_export_core_guard", _EXPORT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_scan_flags_ironclad_dir(tmp_path):
    ex = _load()
    (tmp_path / "sub" / ".ironclad").mkdir(parents=True)
    hits = ex.scan_project_artifacts(tmp_path)
    assert any(h.endswith(".ironclad/") for h in hits), hits


def test_scan_flags_tracks_dir(tmp_path):
    ex = _load()
    (tmp_path / "vault" / ".tracks").mkdir(parents=True)
    assert any(h.endswith(".tracks/") for h in ex.scan_project_artifacts(tmp_path))


def test_scan_flags_registry_json(tmp_path):
    ex = _load()
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / "registry.json").write_text("{}", encoding="utf-8")
    assert any(h.endswith("registry.json") for h in ex.scan_project_artifacts(tmp_path))


def test_scan_clean_dir_is_empty(tmp_path):
    ex = _load()
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "x.md").write_text("ok", encoding="utf-8")
    assert ex.scan_project_artifacts(tmp_path) == []


def test_runtime_artifacts_are_in_copy_ignore():
    ex = _load()
    # shutil.ignore_patterns returns a callable (dir, names) -> set-to-ignore; the runtime machinery names
    # must be dropped so they are never even staged (the scan is the defensive backstop).
    ignored = ex._COPY_IGNORE("somedir", ["alpha.py", ".ironclad", ".tracks", "registry.json", "keep"])
    assert {".ironclad", ".tracks", "registry.json"} <= set(ignored)
    assert "alpha.py" not in ignored and "keep" not in ignored
