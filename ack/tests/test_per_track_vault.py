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


def test_is_safe_track():
    assert gx10._is_safe_track("main")
    assert gx10._is_safe_track("feature-x")
    assert gx10._is_safe_track("v1.2_a")
    assert not gx10._is_safe_track("")
    assert not gx10._is_safe_track(".")
    assert not gx10._is_safe_track("..")
    assert not gx10._is_safe_track("a/b")
    assert not gx10._is_safe_track("a\\b")


def test_active_track_defaults_to_main_without_ctx():
    assert pc.current() is None
    assert gx10._active_track() == "main"


def test_active_track_from_ctx():
    with pc.use(ProjectContext("p", "/proj", "ns", track="feature")):
        assert gx10._active_track() == "feature"


def test_active_track_unsafe_falls_back_to_main():
    with pc.use(ProjectContext("p", "/proj", "ns", track="../evil")):
        assert gx10._active_track() == "main"


def test_vault_root_no_ctx_is_relative_vault(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert gx10.vault_root() == Path("vault")


def test_vault_root_main_track_byte_identical(tmp_path):
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        assert gx10.vault_root() == Path(str(tmp_path)) / "vault"


def test_vault_root_absolute_override_with_track(monkeypatch, tmp_path):
    # an absolute VAULT_ROOT override is taken as-is for main, and a non-main track is still isolated
    # under it (the track subtree applies in the absolute-override branch too).
    abs_vault = tmp_path / "abs_vault"
    monkeypatch.setattr(gx10, "VAULT_ROOT", str(abs_vault))
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        assert gx10.vault_root() == abs_vault
    with pc.use(ProjectContext("p", str(tmp_path), "ns", track="feature")):
        assert gx10.vault_root() == abs_vault / ".tracks" / "feature"


def test_vault_root_non_main_track_is_isolated_subtree(tmp_path):
    with pc.use(ProjectContext("p", str(tmp_path), "ns", track="feature-x")):
        assert gx10.vault_root() == Path(str(tmp_path)) / "vault" / ".tracks" / "feature-x"


def test_vault_root_unsafe_track_resolves_to_main(tmp_path):
    with pc.use(ProjectContext("p", str(tmp_path), "ns", track="..")):
        assert gx10.vault_root() == Path(str(tmp_path)) / "vault"


def test_two_tracks_have_distinct_subtrees(tmp_path):
    with pc.use(ProjectContext("p", str(tmp_path), "ns", track="a")):
        a = gx10.vault_root()
    with pc.use(ProjectContext("p", str(tmp_path), "ns", track="b")):
        b = gx10.vault_root()
    assert a != b
    assert a.name == "a" and b.name == "b"


def test_initiative_isolated_per_track(tmp_path):
    with pc.use(ProjectContext("p", str(tmp_path), "ns", track="feature")):
        v = gx10.initiative_new("Track Work", "software")
        assert (Path(str(tmp_path)) / "vault" / ".tracks" / "feature" / v.slug / "meta.md").is_file()
        assert [iv.slug for iv in gx10.initiative_list()] == [v.slug]
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):  # main track
        assert gx10.initiative_list() == []  # main does not see the feature-track initiative
    assert pc.current() is None
