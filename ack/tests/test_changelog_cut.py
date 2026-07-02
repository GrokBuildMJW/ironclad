"""Deterministic CHANGELOG release cut (#994-S13), offline.

Pure logic only. Lives in `scripts/ci/` (private) → skips in an installed/clean-room tree. Pins: the
[Unreleased] body moves into a fresh `## [X.Y.Z]`, a fresh empty [Unreleased] is opened, the result is
`release-ready` per release_preflight, and the fail-closed refusals (empty / absent / double-cut).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_CC = _REPO / "scripts" / "ci" / "changelog_cut.py"
_RP = _REPO / "scripts" / "ci" / "release_preflight.py"

pytestmark = pytest.mark.skipif(
    not _CC.is_file(),
    reason="private CI changelog_cut.py absent — installed/clean-room tree",
)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


_DIRTY = """# Changelog

## [Unreleased]
- **Feature A** — did a thing
  across two lines.
- **Feature B** — another thing

## [0.0.22]
- shipped thing
"""


def test_cut_moves_body_and_opens_fresh_unreleased():
    cc = _load("_cc", _CC)
    out = cc.cut_changelog(_DIRTY, "0.0.23")
    # a fresh empty [Unreleased] on top, then the new [0.0.23] carrying the moved body, then the old release
    assert out.index("## [Unreleased]") < out.index("## [0.0.23]") < out.index("## [0.0.22]")
    assert "## [0.0.23]" in out and "Feature A" in out and "Feature B" in out
    # the moved entries live under [0.0.23], NOT under [Unreleased]
    unrel = out[out.index("## [Unreleased]"): out.index("## [0.0.23]")]
    assert "Feature A" not in unrel and "- " not in unrel        # fresh [Unreleased] is empty
    # the multi-line entry stayed intact (no duplication — the bug this tool prevents)
    assert out.count("Feature A") == 1 and out.count("across two lines.") == 1


def test_cut_result_is_release_ready_per_preflight():
    cc = _load("_cc2", _CC)
    rp = _load("_rp", _RP)
    out = cc.cut_changelog(_DIRTY, "0.0.23")
    assert rp.changelog_release_state(out, "0.0.23") == "release-ready"


def test_refuses_empty_unreleased():
    cc = _load("_cc3", _CC)
    empty = "# Changelog\n\n## [Unreleased]\n\n## [0.0.22]\n- x\n"
    with pytest.raises(ValueError, match="no entries"):
        cc.cut_changelog(empty, "0.0.23")


def test_refuses_absent_unreleased():
    cc = _load("_cc4", _CC)
    with pytest.raises(ValueError, match="no '## \\[Unreleased\\]'"):
        cc.cut_changelog("# Changelog\n\n## [0.0.22]\n- x\n", "0.0.23")


def test_refuses_double_cut():
    cc = _load("_cc5", _CC)
    with pytest.raises(ValueError, match="already has a section"):
        cc.cut_changelog(_DIRTY, "0.0.22")           # 0.0.22 already released → never double-cut
