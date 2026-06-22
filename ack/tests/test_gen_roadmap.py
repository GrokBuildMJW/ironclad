"""roadmap generator (#176, ADR-0006 Option A): roadmap.md is a derived view of the OPEN phases.

`render_roadmap` is a pure function (tested offline); `fetch_phases` keeps only open milestones with
≥1 open `type/feature` epic (tested with a stubbed `gh`). Lives in `scripts/ci/` (private) → skips in
an installed/clean-room tree.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_GEN = _REPO / "scripts" / "ci" / "gen_roadmap.py"

pytestmark = pytest.mark.skipif(
    not _GEN.is_file(),
    reason="private CI generator (scripts/ci/gen_roadmap.py) absent — installed/clean-room tree",
)


def _load():
    spec = importlib.util.spec_from_file_location("_genroadmap", _GEN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_render_includes_each_open_phase_in_order():
    gen = _load()
    out = gen.render_roadmap([
        {"title": "Alpha", "description": "We plan to build alpha."},
        {"title": "Beta", "description": "Beta is planned next."},
    ])
    assert "# Roadmap" in out and "Forward-looking only" in out
    assert "## Alpha\n\nWe plan to build alpha." in out
    assert "## Beta\n\nBeta is planned next." in out
    assert out.index("## Alpha") < out.index("## Beta")      # order preserved
    assert out.rstrip().endswith("openly-developed project.")  # footer


def test_render_empty_has_no_sections():
    gen = _load()
    out = gen.render_roadmap([])
    assert "# Roadmap" in out and "## " not in out            # header + footer only, no phases


def test_render_is_idempotent():
    gen = _load()
    phases = [{"title": "X", "description": "planned X."}]
    assert gen.render_roadmap(phases) == gen.render_roadmap(phases)


def test_fetch_phases_keeps_open_milestones_with_a_description_sorted(monkeypatch):
    gen = _load()
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    # The API call is `milestones?state=open` → only OPEN milestones are returned; the generator
    # additionally skips any with an empty description (a delivered phase drops by closing its
    # milestone, so it is no longer state=open).
    milestones = [
        {"number": 2, "title": "Beta", "description": "planned beta."},
        {"number": 1, "title": "Alpha", "description": "planned alpha."},
        {"number": 5, "title": "NoDesc", "description": ""},          # open but no narrative → skipped
        {"number": 4, "title": "Gamma", "description": "planned gamma."},
    ]

    def fake_gh(args):
        assert args[0] == "api" and "milestones" in args[1]            # no per-epic query anymore
        return milestones

    monkeypatch.setattr(gen, "_gh", fake_gh)
    phases = gen.fetch_phases()
    assert [p["title"] for p in phases] == ["Alpha", "Beta", "Gamma"]  # sorted by number, NoDesc skipped
    assert [p["number"] for p in phases] == [1, 2, 4]


def test_check_soft_skips_when_gh_unavailable(monkeypatch):
    gen = _load()

    def boom(args):
        raise OSError("gh not found")

    monkeypatch.setattr(gen, "_gh", boom)
    # --check must not crash / fail when GitHub is unreachable (soft-skip, exit 0)
    assert gen.main(["--check"]) == 0
