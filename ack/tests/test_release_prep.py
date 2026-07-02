"""Complete release_prep tool (#994-S14), offline.

Pure logic. Lives in `scripts/ci/` (private) → skips in an installed/clean-room tree. Pins: the bump touches
all version-carrying files AND the result satisfies the REAL invariants it exists to satisfy —
`devloop_dangling_refs == []` (DEV_LOOP), the version string present in status.md, and a release-ready
CHANGELOG per release_preflight — plus the fail-closed refusals.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_RPREP = _REPO / "scripts" / "ci" / "release_prep.py"
_DOCTOR = _REPO / "scripts" / "devprocess" / "doctor.py"
_RPF = _REPO / "scripts" / "ci" / "release_preflight.py"

pytestmark = pytest.mark.skipif(
    not _RPREP.is_file(),
    reason="private CI release_prep.py absent — installed/clean-room tree",
)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _fake_repo(root: Path, ver: str = "0.0.22"):
    (root / "core" / "docs").mkdir(parents=True)
    (root / ".github").mkdir(parents=True)
    (root / "core" / "pyproject.toml").write_text(
        f'[project]\nname = "ironclad-ai"\nversion = "{ver}"\n', encoding="utf-8")
    (root / ".github" / "DEV_LOOP.md").write_text(
        f"# Dev loop\n- **Stand:** Releases via PyPI (`ironclad-ai`, aktuell v{ver}).\n", encoding="utf-8")
    (root / "core" / "docs" / "status.md").write_text(
        f"# Status\n> published as GitHub Releases (latest `v{ver}`); the running state is here.\n",
        encoding="utf-8")
    (root / "core" / "README.md").write_text(
        f"# Ironclad\nGitHub Releases (currently `v{ver}`) — early previews.\n", encoding="utf-8")
    (root / "core" / "CHANGELOG.md").write_text(
        f"# Changelog\n\n## [Unreleased]\n- **A feature** — did a thing\n\n## [{ver}]\n- shipped\n",
        encoding="utf-8")


def test_prepare_release_bumps_all_files_and_satisfies_invariants(tmp_path):
    rp = _load("_rprep", _RPREP)
    doctor = _load("_doctor", _DOCTOR)
    rpf = _load("_rpf2", _RPF)
    _fake_repo(tmp_path)
    changed = rp.prepare_release(tmp_path, "0.0.23")
    assert set(changed) == {"core/pyproject.toml", ".github/DEV_LOOP.md", "docs/status.md",
                            "core/README.md", "core/CHANGELOG.md"}
    py = (tmp_path / "core" / "pyproject.toml").read_text(encoding="utf-8")
    dev = (tmp_path / ".github" / "DEV_LOOP.md").read_text(encoding="utf-8")
    status = (tmp_path / "core" / "docs" / "status.md").read_text(encoding="utf-8")
    readme = (tmp_path / "core" / "README.md").read_text(encoding="utf-8")
    changelog = (tmp_path / "core" / "CHANGELOG.md").read_text(encoding="utf-8")
    # pyproject bumped
    assert 'version = "0.0.23"' in py
    # DEV_LOOP satisfies the REAL invariant it exists for
    assert doctor.devloop_dangling_refs(dev, set(), "0.0.23") == []
    # status.md mentions the current version (the doc-reality gate's exact condition)
    assert "0.0.23" in status
    # README current-version mention updated
    assert "currently `v0.0.23`" in readme
    # CHANGELOG is release-ready per release_preflight (cut + version section non-empty)
    assert rpf.changelog_release_state(changelog, "0.0.23") == "release-ready"


def test_check_mode_validates_without_writing(tmp_path):
    rp = _load("_rprep2", _RPREP)
    _fake_repo(tmp_path)
    before = (tmp_path / "core" / "pyproject.toml").read_text(encoding="utf-8")
    changed = rp.prepare_release(tmp_path, "0.0.23", write=False)
    assert "core/pyproject.toml" in changed                       # planned
    assert (tmp_path / "core" / "pyproject.toml").read_text(encoding="utf-8") == before  # but NOT written


def test_bad_version_refused(tmp_path):
    rp = _load("_rprep3", _RPREP)
    _fake_repo(tmp_path)
    with pytest.raises(ValueError, match="not X.Y.Z"):
        rp.prepare_release(tmp_path, "v0.0.23")


def test_fail_closed_on_absent_pattern(tmp_path):
    rp = _load("_rprep4", _RPREP)
    _fake_repo(tmp_path)
    # drift: DEV_LOOP loses its 'aktuell v<X>' marker → the bump must refuse, not silently skip
    (tmp_path / ".github" / "DEV_LOOP.md").write_text("# Dev loop\nno version here\n", encoding="utf-8")
    with pytest.raises(ValueError, match="DEV_LOOP"):
        rp.prepare_release(tmp_path, "0.0.23")


def test_pure_bump_helpers():
    rp = _load("_rprep5", _RPREP)
    assert rp.bump_devloop("aktuell v0.0.22 today", "0.0.23") == "aktuell v0.0.23 today"
    assert rp.bump_status("latest `v0.0.22`", "0.0.23") == "latest `v0.0.23`"
    assert rp.bump_readme("currently `v0.0.22`", "0.0.23") == "currently `v0.0.23`"
