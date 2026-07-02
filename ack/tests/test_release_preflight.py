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


# ── #348 S7a: trigger-ownership + fail-closed preflight ──
_PUBLISH_YML = _REPO / "core" / ".github" / "workflows" / "publish.yml"


def test_preflight_main_requires_a_tag():
    rp = _load()
    assert rp.main(["--preflight"]) == 2                       # empty tag -> fail-closed (was a skip path)


def test_preflight_main_fails_closed_on_unreachable_pypi(monkeypatch):
    # a transient PyPI blip must NOT no-op the duplicate guard: an unresolvable check is now a refusal.
    rp = _load()
    monkeypatch.setattr(rp, "_pypi_versions", lambda *a, **k: ([], False))   # index unreachable (#397: accepts index_base)
    monkeypatch.setattr(rp, "release_preflight", lambda *a, **k: [])         # tag/version/changelog all agree
    assert rp.main(["--preflight", "--tag", "v9.9.9"]) == 1                  # unreachable -> fail-closed
    # control: reachable + all-agree passes
    monkeypatch.setattr(rp, "_pypi_versions", lambda *a, **k: (["0.0.1"], True))
    assert rp.main(["--preflight", "--tag", "v9.9.9"]) == 0


def test_preflight_index_url_routes_the_duplicate_check(monkeypatch):
    # #397 S14c: --index-url threads the JSON-API base into the duplicate check so a Test-PyPI cut checks
    # Test-PyPI (not production). The default is production.
    rp = _load()
    seen = {}
    monkeypatch.setattr(rp, "_pypi_versions", lambda base=rp.PYPI_JSON_BASE: (seen.__setitem__("base", base) or ([], True)))
    monkeypatch.setattr(rp, "release_preflight", lambda *a, **k: [])
    rp.main(["--preflight", "--tag", "v9.9.9", "--index-url", rp.TESTPYPI_JSON_BASE])
    assert seen["base"] == rp.TESTPYPI_JSON_BASE                              # routed to Test-PyPI
    rp.main(["--preflight", "--tag", "v9.9.9"])
    assert seen["base"] == rp.PYPI_JSON_BASE                                  # default = production


def test_pypi_versions_404_is_first_publish_other_errors_unreachable(monkeypatch):
    # #407: a 404 (project not on the index yet) is the FIRST publish -> ([], reachable=True), NOT
    # fail-closed; any other HTTP status + network errors stay unreachable ([], False).
    import urllib.error
    rp = _load()

    def _raise(code_or_exc):
        def _f(url, timeout=0):
            if isinstance(code_or_exc, int):
                raise urllib.error.HTTPError(url, code_or_exc, "x", {}, None)
            raise code_or_exc
        return _f

    monkeypatch.setattr(rp.urllib.request, "urlopen", _raise(404))
    assert rp._pypi_versions("https://test.pypi.org/pypi") == ([], True)     # fresh index -> safe first publish
    monkeypatch.setattr(rp.urllib.request, "urlopen", _raise(503))
    assert rp._pypi_versions() == ([], False)                                # server error -> unreachable (fail-closed)
    monkeypatch.setattr(rp.urllib.request, "urlopen", _raise(urllib.error.URLError("dns")))
    assert rp._pypi_versions() == ([], False)                                # network down -> unreachable (fail-closed)


def test_publish_yml_only_release_triggered_and_failcloses_empty_tag():
    # workflow_dispatch (the ungated, tagless manual-publish bypass) is removed; the only trigger is
    # release:published, and the inline preflight refuses an empty tag.
    import yaml
    text = _PUBLISH_YML.read_text(encoding="utf-8")
    doc = yaml.safe_load(text)
    triggers = doc["on"] if "on" in doc else doc[True]         # PyYAML maps a bare `on:` key to True
    assert "workflow_dispatch" not in triggers                 # the ungated trigger is gone (key, not comment)
    assert "release" in triggers and triggers["release"]["types"] == ["published"]
    assert 'if [ -z "${TAG:-}" ]' in text                      # fail-closed on an empty tag


def test_inline_publish_preflight_agrees_with_release_preflight_on_tag_version():
    # equivalence pin (charter fork-5 fallback): publish.yml's portable inline preflight and the engine's
    # release_preflight.py enforce the SAME tag==pyproject-version invariant, so the two cannot drift.
    rp = _load()
    text = _PUBLISH_YML.read_text(encoding="utf-8")
    assert '"${TAG#v}" != "${VER}"' in text                    # inline: tag (sans v) must equal version
    assert rp.release_preflight("v0.0.14", "0.0.15", _RELEASED, []) , "release_preflight must flag a mismatch"
    assert rp.release_preflight("v0.0.15", "0.0.15", _RELEASED, []) == []   # agree -> pass


# ── #994-S6 (C0-6/C0-7): staged-release guards ──
def test_staging_route_ok_requires_test_pypi_first():
    rp = _load()
    assert rp.staging_route_ok("https://test.pypi.org/legacy/") is True    # test-PyPI first → ok
    assert rp.staging_route_ok("https://upload.pypi.org/legacy/") is False # straight to production → refuse
    assert rp.staging_route_ok("") is False                                # empty = production default → refuse


def test_main_safe_reasons_blocks_a_pending_rollback_not_reverted():
    rp = _load()
    assert rp.main_safe_reasons(rollback_pending=False, main_reverted=False) == []   # nothing rolled back
    assert rp.main_safe_reasons(rollback_pending=True, main_reverted=True) == []      # rolled back + reverted
    reasons = rp.main_safe_reasons(rollback_pending=True, main_reverted=False)
    assert reasons and "re-ship the rolled-back break" in reasons[0]                  # fail-closed
