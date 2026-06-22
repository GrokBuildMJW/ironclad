"""Release-version invariant + release-aware CHANGELOG gate (#198, #177, ADR-0007), offline.

Pure logic only (no network, no gh). Lives in `scripts/ci/` (private) → skips in an installed/
clean-room tree. Pins: the #177 reconciliation (both dev and release CHANGELOG states pass the gate,
drift fails) and the fail-closed pre-publish assertion (tag == pyproject, release-ready, not on PyPI).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_RP = _REPO / "scripts" / "ci" / "release_preflight.py"

pytestmark = pytest.mark.skipif(
    not _RP.is_file(),
    reason="private CI release_preflight.py absent — installed/clean-room tree",
)


def _load():
    spec = importlib.util.spec_from_file_location("_relpf", _RP)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


_DEV = """# Changelog
## [Unreleased]
### Added
- some pending work

## [0.0.14] - 2026-06-22
### Added
- shipped thing
"""

_RELEASED = """# Changelog
## [Unreleased]

## [0.0.15] - 2026-06-23
### Added
- the new release's entry

## [0.0.14] - 2026-06-22
### Added
- old
"""

_DRIFT_EMPTY = """# Changelog
## [Unreleased]

## [0.0.14] - 2026-06-22
### Added
- old
"""  # [Unreleased] empty, but pyproject is 0.0.15 → newest (0.0.14) != pyproject → drift

_DRIFT_EMPTY_SECTION = """# Changelog
## [Unreleased]

## [0.0.15] - 2026-06-23

## [0.0.14] - 2026-06-22
### Added
- old
"""  # newest section matches pyproject but is EMPTY → no ship without an entry


def test_dev_state_is_unreleased_dirty():
    rp = _load()
    assert rp.changelog_release_state(_DEV, "0.0.14") == "unreleased-dirty"
    assert rp.changelog_gate_ok("unreleased-dirty") is True   # #177: pending dev work passes the gate


def test_bumped_state_is_release_ready():
    rp = _load()
    assert rp.changelog_release_state(_RELEASED, "0.0.15") == "release-ready"
    assert rp.changelog_gate_ok("release-ready") is True      # #177: a cut release ALSO passes the gate


def test_empty_unreleased_with_version_mismatch_is_drift():
    rp = _load()
    assert rp.changelog_release_state(_DRIFT_EMPTY, "0.0.15") == "drift"
    assert rp.changelog_gate_ok("drift") is False


def test_empty_newest_section_is_drift():
    rp = _load()
    # newest == pyproject but the section has no entry → still no ship without a changelog entry
    assert rp.changelog_release_state(_DRIFT_EMPTY_SECTION, "0.0.15") == "drift"


def test_newest_released_version_ignores_unreleased():
    rp = _load()
    assert rp.newest_released_version(_RELEASED) == "0.0.15"
    assert rp.newest_released_version(_DEV) == "0.0.14"


def test_preflight_passes_only_when_all_three_views_agree():
    rp = _load()
    # release-ready, tag matches, not on PyPI → clean
    assert rp.release_preflight("v0.0.15", "0.0.15", _RELEASED, ["0.0.14", "0.0.13"]) == []


def test_preflight_flags_tag_mismatch_changelog_and_duplicate():
    rp = _load()
    fails = rp.release_preflight("v0.0.14", "0.0.15", _DEV, ["0.0.14"])
    assert any("tag" in f and "pyproject" in f for f in fails)            # tag != pyproject
    assert any("release-ready" in f for f in fails)                       # dev changelog, not cut
    # 0.0.15 not in the pypi list here → no duplicate finding for it
    assert not any("ALREADY on PyPI" in f for f in fails)


def test_preflight_blocks_a_version_already_on_pypi():
    rp = _load()
    fails = rp.release_preflight("v0.0.15", "0.0.15", _RELEASED, ["0.0.15"])
    assert len(fails) == 1 and "ALREADY on PyPI" in fails[0]


def test_tag_normalisation_strips_leading_v():
    rp = _load()
    assert rp.release_preflight("0.0.15", "0.0.15", _RELEASED, []) == []   # bare tag, no 'v'
