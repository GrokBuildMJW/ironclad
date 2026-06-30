from __future__ import annotations
import json
import os
import sys
import types
from pathlib import Path
import pytest

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=lambda **kw: object()))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import project_registry as pr   # noqa: E402
import project_context as pc    # noqa: E402
import gx10                     # noqa: E402


class FakeGx:
    def __init__(self):
        self.messages = [{"role": "system", "content": "SYS"}]
        self.last_response = ""
        self.fail_save = False

    def save_session(self, *, strict=False):
        if self.fail_save:
            if strict:
                raise OSError("disk full")
            return
        p = gx10.session_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"messages": self.messages}), encoding="utf-8")

    def load_session(self):
        p = gx10.session_path()
        if not p.exists():
            return 0
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return 0
        loaded = [m for m in data.get("messages", []) if m.get("role") != "system"]
        system = next((m for m in self.messages if m.get("role") == "system"), None)
        self.messages = ([system] if system else []) + loaded
        return len(loaded)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("GX10_HOME", str(tmp_path / "home"))
    wd = tmp_path / "wd"
    wd.mkdir()
    monkeypatch.chdir(wd)
    gx10._REGISTRY = None
    gx10._ACTIVE_PROJECT = None
    gx10._BOOT_WORKDIR = None
    pc.set_current(None)
    gx10.init_registry(wd)
    gx10._BASE_CFG = gx10._code_defaults()
    monkeypatch.setattr(gx10, "STORE", None)
    monkeypatch.setattr(gx10, "_load_skills", lambda *a, **k: None)
    yield wd
    gx10._REGISTRY = None
    gx10._ACTIVE_PROJECT = None
    gx10._BOOT_WORKDIR = None
    pc.set_current(None)


# ---------------------------------------------------------------------------
# Registry archive persistence
# ---------------------------------------------------------------------------

def test_set_archived_roundtrip(tmp_path):
    reg = pr.Registry(home=tmp_path / ".h")
    reg.register("p1", str(tmp_path / "p1"))
    assert reg.get("p1").archived is False
    reg.set_archived("p1", True)
    assert reg.get("p1").archived is True
    reg.set_archived("p1", False)
    assert reg.get("p1").archived is False


def test_set_archived_unknown_raises(tmp_path):
    reg = pr.Registry(home=tmp_path / ".h")
    reg.ensure_default(tmp_path)
    with pytest.raises(KeyError):
        reg.set_archived("nope", True)


def test_new_project_not_archived(tmp_path):
    reg = pr.Registry(home=tmp_path / ".h")
    proj = reg.register("p1", str(tmp_path / "p1"))
    assert proj.archived is False


def test_remove_expected_root_match_removes_and_returns(tmp_path):
    reg = pr.Registry(home=tmp_path / ".h")
    proj = reg.register("p1", str(tmp_path / "p1"))
    removed = reg.remove("p1", expected_root=proj.root)
    assert removed is not None and removed.id == "p1"
    assert reg.get("p1") is None


def test_remove_expected_root_mismatch_is_noop(tmp_path):
    reg = pr.Registry(home=tmp_path / ".h")
    reg.register("p1", str(tmp_path / "p1"))
    assert reg.remove("p1", expected_root=str(tmp_path / "other")) is None
    assert reg.get("p1") is not None     # not removed when the root does not match (atomic against reuse)


# ---------------------------------------------------------------------------
# gx10 helper functions
# ---------------------------------------------------------------------------

def test_project_scopes_main_only():
    proj = types.SimpleNamespace(mem_ns="ns", tracks=["main"])
    assert gx10._project_scopes(proj) == ["ns"]


def test_project_scopes_with_tracks():
    proj = types.SimpleNamespace(mem_ns="ns", tracks=["main", "feat"])
    assert gx10._project_scopes(proj) == ["ns", "ns::track::feat"]


def test_project_scopes_empty_ns():
    proj = types.SimpleNamespace(mem_ns="", tracks=["main"])
    assert gx10._project_scopes(proj) == []


def test_safe_to_purge_refuses_cwd_and_ancestor_and_missing(env):
    wd = env
    assert gx10._safe_to_purge(str(wd / "missing"))[0] is False
    assert gx10._safe_to_purge(str(wd))[0] is False
    assert gx10._safe_to_purge(str(wd.parent))[0] is False
    assert gx10._safe_to_purge(str(Path.home().resolve()))[0] is False


def test_safe_to_purge_allows_contained_dir(env):
    sub = env / "proj"
    sub.mkdir()
    assert gx10._safe_to_purge(str(sub)) == (True, "")


# ---------------------------------------------------------------------------
# /project archive and /project unarchive
# ---------------------------------------------------------------------------

def test_archive_refuses_active(env):
    gx10._project_command("new alpha", FakeGx())
    gx10._project_command("new beta", FakeGx())
    assert gx10._ACTIVE_PROJECT.id == "beta"
    out = gx10._project_command("archive beta", FakeGx())
    assert "ACTIVE" in out
    assert gx10._REGISTRY.get("beta").archived is False


def test_archive_refuses_default(env):
    out = gx10._project_command("archive default", FakeGx())
    assert "default" in out
    assert "cannot" in out


def test_archive_inactive_hides_from_list(env):
    gx10._project_command("new alpha", FakeGx())
    gx10._project_command("new beta", FakeGx())
    out = gx10._project_command("archive alpha", FakeGx())
    assert "archived" in out
    list_out = gx10._project_command("list", FakeGx())
    assert "alpha" not in list_out
    assert "beta" in list_out


def test_list_all_shows_archived(env):
    gx10._project_command("new alpha", FakeGx())
    gx10._project_command("new beta", FakeGx())
    gx10._project_command("archive alpha", FakeGx())
    list_out = gx10._project_command("list --all", FakeGx())
    assert "alpha" in list_out
    assert "[archived]" in list_out


def test_unarchive_restores(env):
    gx10._project_command("new alpha", FakeGx())
    gx10._project_command("new beta", FakeGx())
    gx10._project_command("archive alpha", FakeGx())
    out = gx10._project_command("unarchive alpha", FakeGx())
    assert "un-archived" in out
    list_out = gx10._project_command("list", FakeGx())
    assert "alpha" in list_out
    assert "[archived]" not in list_out


def test_switch_to_archived_refused(env):
    gx10._project_command("new alpha", FakeGx())
    gx10._project_command("new beta", FakeGx())
    gx10._project_command("archive alpha", FakeGx())
    out = gx10._switch_command(FakeGx(), "alpha")
    assert "is archived" in out


# ---------------------------------------------------------------------------
# /project delete
# ---------------------------------------------------------------------------

def test_delete_default_refused(env):
    out = gx10._project_command("delete default", FakeGx())
    assert "default" in out
    assert "cannot" in out


def test_delete_unknown(env):
    out = gx10._project_command("delete nope", FakeGx())
    assert "unknown project" in out


def test_delete_inactive_keeps_dir(env):
    wd = env
    gx10._project_command("new alpha", FakeGx())
    gx10._project_command("new beta", FakeGx())
    alpha_dir = wd / "alpha"
    assert alpha_dir.exists()
    out = gx10._project_command("delete alpha", FakeGx())
    assert "deleted" in out
    assert not any(p.id == "alpha" for p in gx10._REGISTRY.list())
    assert alpha_dir.exists()


def test_delete_active_switches_to_default(env):
    gx10._project_command("new gamma", FakeGx())
    assert gx10._ACTIVE_PROJECT.id == "gamma"
    out = gx10._project_command("delete gamma", FakeGx())
    assert "deleted" in out
    assert gx10._ACTIVE_PROJECT.id == "default"
    assert not any(p.id == "gamma" for p in gx10._REGISTRY.list())


def test_delete_active_requires_agent(env):
    gx10._project_command("new gamma", FakeGx())
    assert gx10._ACTIVE_PROJECT.id == "gamma"
    out = gx10._project_command("delete gamma")
    assert "requires an interactive session" in out
    assert any(p.id == "gamma" for p in gx10._REGISTRY.list())


def test_delete_purge_removes_dir(env):
    wd = env
    gx10._project_command("new alpha", FakeGx())
    gx10._project_command("new beta", FakeGx())
    alpha_dir = wd / "alpha"
    assert alpha_dir.exists()
    out = gx10._project_command("delete alpha --purge", FakeGx())
    assert "deleted" in out
    assert not alpha_dir.exists()
