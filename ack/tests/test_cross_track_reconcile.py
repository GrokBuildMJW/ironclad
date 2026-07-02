from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=lambda **kw: object()))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import project_context as pc
from project_context import ProjectContext
import gx10


def test_project_vault_base_is_track_independent(tmp_path):
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        assert gx10._project_vault_base() == Path(str(tmp_path)) / "vault"
    with pc.use(ProjectContext("p", str(tmp_path), "ns", track="feature")):
        assert gx10._project_vault_base() == Path(str(tmp_path)) / "vault"


def test_project_tracks_main_only_when_no_subtree(tmp_path):
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        gx10.initiative_new("Main Work", "software")
        assert gx10._project_tracks() == ["main"]


def test_project_tracks_lists_main_plus_sorted_tracks(tmp_path):
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        gx10.initiative_new("M", "software")
    with pc.use(ProjectContext("p", str(tmp_path), "ns", track="b")):
        gx10.initiative_new("B", "software")
    with pc.use(ProjectContext("p", str(tmp_path), "ns", track="a")):
        gx10.initiative_new("A", "software")
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        assert gx10._project_tracks() == ["main", "a", "b"]


def test_reconcile_active_project_reconciles_all_in_track(tmp_path):
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        gx10.initiative_new("One", "software")
        gx10.initiative_new("Two", "software")
        out = gx10.reconcile_active_project(links=False)
        assert len(out) == 2
        assert all("indexed" in r for r in out)


def test_reconcile_all_tracks_covers_every_track(tmp_path):
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        mv = gx10.initiative_new("Main Work", "software")
    with pc.use(ProjectContext("p", str(tmp_path), "ns", track="feature")):
        fv = gx10.initiative_new("Feat Work", "software")
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        res = gx10.reconcile_all_tracks(links=False)
        assert set(res.keys()) == {"main", "feature"}
        assert len(res["main"]) == 1 and len(res["feature"]) == 1
    assert (Path(str(tmp_path)) / "vault" / mv.slug / "INDEX.md").is_file()
    assert (Path(str(tmp_path)) / "vault" / ".tracks" / "feature" / fv.slug / "INDEX.md").is_file()


def test_reconcile_all_tracks_no_active_project_main_only(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert pc.current() is None
    res = gx10.reconcile_all_tracks(links=False)
    assert list(res.keys()) == ["main"]


def test_reconcile_all_tracks_no_active_ignores_stray_tracks_dir(monkeypatch, tmp_path):
    # a legacy/stray vault/.tracks/<t>/ subtree must NOT surface with no active project: only main
    monkeypatch.chdir(tmp_path)
    (tmp_path / "vault" / ".tracks" / "feature").mkdir(parents=True)
    assert pc.current() is None
    res = gx10.reconcile_all_tracks(links=False)
    assert list(res.keys()) == ["main"]


def test_initiative_reconcile_all_command(tmp_path):
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        gx10.initiative_new("Main Work", "software")
        out = gx10._initiative_command("reconcile all")
        assert "reconcile all" in out and "track" in out.lower()
