from __future__ import annotations
import sys, types
from pathlib import Path
import pytest

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=lambda **kw: object()))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import project_registry as pr
import gx10


# --------------------------------------------------------------------------- #
# Registry-level tests for project tracks
# --------------------------------------------------------------------------- #

def test_add_track_appends_and_is_idempotent(tmp_path: Path) -> None:
    reg = pr.Registry(home=tmp_path)
    reg.register("p1", str(tmp_path / "p1root"))
    reg.add_track("p1", "feat")
    project = reg.get("p1")
    assert project.tracks == ["main", "feat"]
    assert project.active_track == "main"
    # Idempotent second call
    reg.add_track("p1", "feat")
    project = reg.get("p1")
    assert project.tracks == ["main", "feat"]
    assert project.active_track == "main"


def test_add_track_unsafe_raises(tmp_path: Path) -> None:
    reg = pr.Registry(home=tmp_path)
    reg.register("p1", str(tmp_path / "p1root"))
    with pytest.raises(ValueError):
        reg.add_track("p1", "bad/track")
    with pytest.raises(ValueError):
        reg.add_track("p1", "..")


def test_add_track_unknown_project_raises(tmp_path: Path) -> None:
    reg = pr.Registry(home=tmp_path)
    reg.register("p1", str(tmp_path / "p1root"))
    with pytest.raises(KeyError):
        reg.add_track("nope", "feat")


def test_set_active_track_switches(tmp_path: Path) -> None:
    reg = pr.Registry(home=tmp_path)
    reg.register("p1", str(tmp_path / "p1root"))
    reg.add_track("p1", "feat")
    reg.set_active_track("p1", "feat")
    assert reg.get("p1").active_track == "feat"


def test_set_active_track_unregistered_raises(tmp_path: Path) -> None:
    reg = pr.Registry(home=tmp_path)
    reg.register("p1", str(tmp_path / "p1root"))
    with pytest.raises(ValueError):
        reg.set_active_track("p1", "ghost")


def test_set_active_track_unsafe_and_unknown_project(tmp_path: Path) -> None:
    reg = pr.Registry(home=tmp_path)
    reg.register("p1", str(tmp_path / "p1root"))
    with pytest.raises(ValueError):
        reg.set_active_track("p1", "b d")
    with pytest.raises(KeyError):
        reg.set_active_track("nope", "main")


# --------------------------------------------------------------------------- #
# gx10 command tests for project tracks
# --------------------------------------------------------------------------- #

@pytest.fixture
def gx10_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Fresh registry/project with gx10 globals monkeypatched for command tests."""
    reg = pr.Registry(home=tmp_path)
    reg.register("p1", str(tmp_path / "p1root"))
    monkeypatch.setattr(gx10, "_REGISTRY", reg)
    monkeypatch.setattr(gx10, "_ACTIVE_PROJECT", reg.get("p1"))
    monkeypatch.setattr(gx10, "bind_active", lambda: None)
    monkeypatch.setattr(gx10, "_load_skills", lambda *a, **k: None)
    return reg


def test_track_list_shows_active(gx10_env: pr.Registry) -> None:
    result = gx10._project_command("track list")
    assert "main" in result
    assert "[active]" in result       # #1238: markdown-safe [active] tag (was a "* " marker)


def test_track_new_creates_and_switches(gx10_env: pr.Registry) -> None:
    result = gx10._project_command("track new feat")
    assert "feat" in result
    assert "created" in result
    assert "feat" in gx10_env.get("p1").tracks
    assert gx10_env.get("p1").active_track == "feat"
    assert gx10._ACTIVE_PROJECT is not None
    assert gx10._ACTIVE_PROJECT.active_track == "feat"


def test_track_use_existing(gx10_env: pr.Registry) -> None:
    gx10_env.add_track("p1", "feat")
    gx10._ACTIVE_PROJECT = gx10_env.get("p1")
    gx10._project_command("track use feat")
    assert gx10_env.get("p1").active_track == "feat"


def test_track_use_unknown_returns_error(gx10_env: pr.Registry) -> None:
    result = gx10._project_command("track use ghost")
    assert "unknown track" in result
    assert gx10_env.get("p1").active_track == "main"


def test_track_no_active_project(gx10_env: pr.Registry, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gx10, "_ACTIVE_PROJECT", None)
    result = gx10._project_command("track list")
    assert "no active project" in result


def test_track_new_missing_arg_usage(gx10_env: pr.Registry) -> None:
    result = gx10._project_command("track new")
    assert "usage:" in result
