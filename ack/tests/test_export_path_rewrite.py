"""Export path-rewrite (epic #710 sweep): `core/<subdir>/…` references must be rewritten to the
export-relative form for EVERY published top-level subdir — `install/` and `.github/` were missed, so
a `install/ironclad.ps1` reference stayed wrong in the published tree (the file ships at
`install/ironclad.ps1`). These pin the rewrite and guard the published-subdir list so a new dir can't be
silently missed again.

`export_core.py` lives in `scripts/ci/` (private, not exported), so this **skips** on an installed/
clean-room tree where it is absent (mirrors `test_export_leak_guard.py`).
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

# Local build / cache / runtime dirs that may sit under core/ but are never part of the published export.
_LOCAL = {".venv", "venv", ".export", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
          ".ironclad", ".tracks", "build", "dist", "node_modules", ".git"}


def _load():
    spec = importlib.util.spec_from_file_location("_export_core_rewrite", _EXPORT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_rewrite_strips_core_install_and_github_prefixes(tmp_path):
    ex = _load()
    f = tmp_path / "DOC.md"
    f.write_text("see `install/ironclad.ps1`, .github/workflows/ci.yml, and ack/gate.py\n",
                 encoding="utf-8")
    ex.rewrite_core_paths(tmp_path)
    out = f.read_text(encoding="utf-8")
    assert "install/ironclad.ps1" in out and "install/" not in out
    assert ".github/workflows/ci.yml" in out and ".github/" not in out
    assert "ack/gate.py" in out and "ack/" not in out   # a pre-existing subdir still rewritten


def test_rewrite_strips_sibling_parent_from_staged_root_client_link(tmp_path):
    ex = _load()
    f = tmp_path / "README.md"
    f.write_text("[x](../../clients/ink/)\n", encoding="utf-8")
    ex.rewrite_core_paths(tmp_path)
    assert f.read_text(encoding="utf-8") == "[x](clients/ink/)\n"


def test_rewrite_strips_sibling_parent_from_staged_root_skill_link(tmp_path):
    ex = _load()
    f = tmp_path / "README.md"
    f.write_text("[x](../../skills/mpr)\n", encoding="utf-8")
    ex.rewrite_core_paths(tmp_path)
    assert f.read_text(encoding="utf-8") == "[x](skills/mpr)\n"


def test_rewrite_keeps_one_sibling_parent_at_staged_depth_one(tmp_path):
    ex = _load()
    f = tmp_path / "docs" / "README.md"
    f.parent.mkdir()
    f.write_text("[x](../../clients/ink/)\n", encoding="utf-8")
    ex.rewrite_core_paths(tmp_path)
    assert f.read_text(encoding="utf-8") == "[x](../../clients/ink/)\n"


def test_rewrite_keeps_two_sibling_parents_at_staged_depth_two(tmp_path):
    ex = _load()
    f = tmp_path / "docs" / "nested" / "README.md"
    f.parent.mkdir(parents=True)
    f.write_text("[x](../../clients/ink/)\n", encoding="utf-8")
    ex.rewrite_core_paths(tmp_path)
    assert f.read_text(encoding="utf-8") == "[x](../../clients/ink/)\n"


def test_published_subdirs_covers_every_core_topdir():
    # every real published top-level subdir of core/ must be in _PUBLISHED_SUBDIRS, else the path-rewrite
    # would leave `core/<dir>/…` references wrong in the export (the exact bug for install/).
    ex = _load()
    core = _REPO / "core"
    actual = {d.name for d in core.iterdir()
              if d.is_dir() and d.name not in _LOCAL and not d.name.endswith(".egg-info")}
    missing = actual - set(ex._PUBLISHED_SUBDIRS)
    assert not missing, (
        f"core/ has published top-level dir(s) not in export_core._PUBLISHED_SUBDIRS "
        f"(the path-rewrite would miss them): {sorted(missing)}")
    ink_artifacts = {".pytest_cache", ".mypy_cache", ".ruff_cache", "build", ".venv", "venv"}
    assert set(ex._INK_IGNORE("clients/ink", sorted(ink_artifacts))) == ink_artifacts
