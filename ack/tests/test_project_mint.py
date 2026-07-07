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


# ---------------------------------------------------------------------------
# Parser tests for gx10._parse_project_new  (no engine env needed)
# ---------------------------------------------------------------------------

def test_parse_name_only():
    # #984: one type (software); the parser always reports it.
    name, typ, path, err = gx10._parse_project_new("myproj")
    assert (name, typ, path, err) == ("myproj", "software", None, None)


def test_parse_type_is_ignored():
    # #984: --type is dropped — any value (incl. a legacy 'mpr' or a bogus one) is tolerated + ignored,
    # never validated; the project is always software.
    assert gx10._parse_project_new("myproj --type software")[:3] == ("myproj", "software", None)
    assert gx10._parse_project_new("myproj --type MPR")[:3] == ("myproj", "software", None)
    assert gx10._parse_project_new("myproj --type nope")[:3] == ("myproj", "software", None)


def test_parse_quoted_path_with_space():
    name, typ, path, err = gx10._parse_project_new('foo --type mpr --path "/a/b c"')
    assert (name, typ, path, err) == ("foo", "software", "/a/b c", None)


def test_parse_path_simple():
    assert gx10._parse_project_new("foo --path /x/y")[:3] == ("foo", "software", "/x/y")


def test_parse_multiword_name():
    assert gx10._parse_project_new("my cool project")[:3] == ("my cool project", "software", None)


def test_parse_empty_is_usage():
    name, typ, path, err = gx10._parse_project_new("")
    assert name is None and err is not None and err.lower().startswith("usage")


def test_parse_bare_path_flag_rejected():
    # a bare --path (no value) must fail closed; a bare/legacy --type is tolerated + ignored (#984).
    for bad in ("foo --path", "foo --path="):
        name, typ, path, err = gx10._parse_project_new(bad)
        assert name is None and err is not None and "needs a value" in err, bad
    for ok in ("foo --type", "foo --type=", "foo --type mpr"):
        name, typ, path, err = gx10._parse_project_new(ok)
        assert (name, typ, err) == ("foo", "software", None), ok


# ---------------------------------------------------------------------------
# Mint integration tests for gx10._project_command("new ...")
# The mint activates through the real quiesced switch, so the env mirrors the
# switch-command env and the command is driven with a FakeGx agent.
# ---------------------------------------------------------------------------

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
def mint_env(tmp_path, monkeypatch):
    """A full engine env (registry + base config) so the mint's quiesced switch can run; mirrors the
    switch-command test env. Globals set by init_registry are reset on teardown."""
    monkeypatch.setenv("GX10_HOME", str(tmp_path / "home"))
    wd = tmp_path / "wd"
    wd.mkdir()
    monkeypatch.chdir(wd)
    gx10._REGISTRY = None
    gx10._ACTIVE_PROJECT = None
    gx10._BOOT_WORKDIR = None
    pc.set_current(None)
    gx10.init_registry(wd)                          # registry + default project + boot bind
    gx10._BASE_CFG = gx10._code_defaults()
    monkeypatch.setattr(gx10, "STORE", None)
    monkeypatch.setattr(gx10, "_load_skills", lambda *a, **k: None)
    yield wd
    gx10._REGISTRY = None
    gx10._ACTIVE_PROJECT = None
    gx10._BOOT_WORKDIR = None
    pc.set_current(None)


def test_mint_creates_registry_project_at_cwd_slug(mint_env):
    wd = mint_env
    out = gx10._project_command("new alpha", FakeGx())
    assert (wd / "alpha").exists()
    assert any(p.id == "alpha" for p in gx10._REGISTRY.list())
    assert gx10._ACTIVE_PROJECT is not None and gx10._ACTIVE_PROJECT.id == "alpha"
    assert gx10._ACTIVE_PROJECT.mem_ns
    assert "created" in out and "active" in out
    assert "now on alpha" in out          # activation went through the real switch


def test_project_list_active_marker_is_markdown_safe(mint_env):
    # #1238: the active project must carry the [active] tag the legend advertises. A leading "* " marker
    # collided with the client's markdown renderer (became a generic "- " bullet, dropping the marker).
    gx10._project_command("new alpha", FakeGx())          # alpha becomes active
    out = gx10._project_command("list", FakeGx())
    assert "([active] = current" in out                    # legend uses the [active] tag …
    assert "- alpha [active]" in out                       # … and the active row carries it
    assert "* alpha" not in out and "(* = active)" not in out   # the markdown-colliding "* " marker is gone


def test_mint_seeds_software_unit(mint_env):
    # #984: /project new always seeds a software unit (no --type needed).
    wd = mint_env
    out = gx10._project_command("new beta", FakeGx())
    assert "seeded software" in out
    assert list((wd / "beta").rglob("meta.md"))   # the seeded vault unit under the new project root


def test_mint_with_path_roots_there(mint_env):
    wd = mint_env
    target = wd / "custom"
    gx10._project_command(f"new gamma --path {target}", FakeGx())
    assert any(Path(p.root) == target.resolve() for p in gx10._REGISTRY.list())


def test_mint_activation_resets_conversation(mint_env):
    # a mid-session mint must NOT bleed the old conversation into the new project (it switches)
    ag = FakeGx()
    ag.messages.append({"role": "user", "content": "old-default-turn"})
    gx10._project_command("new delta", ag)
    assert ag.messages == [{"role": "system", "content": "SYS"}]


def test_mint_duplicate_failclosed(mint_env):
    gx10._project_command("new dup", FakeGx())
    out2 = gx10._project_command("new dup", FakeGx())
    assert "already registered" in out2
    assert sum(1 for p in gx10._REGISTRY.list() if p.id == "dup") == 1


def test_mint_ignores_legacy_type(mint_env):
    # #984: a legacy/bogus --type is tolerated + ignored — the project is created as software.
    out = gx10._project_command("new eps --type bogus", FakeGx())
    assert "created" in out and "seeded software" in out
    assert any(p.id == "eps" for p in gx10._REGISTRY.list())


def test_mint_bad_name_rejected(mint_env):
    out = gx10._project_command("new !!!", FakeGx())
    assert "invalid name" in out
    assert not any(p.id == "initiative" for p in gx10._REGISTRY.list())


def test_mint_duplicate_with_path_leaves_no_orphan_dir(mint_env):
    wd = mint_env
    gx10._project_command("new dup", FakeGx())
    fresh = wd / "freshdup"
    out2 = gx10._project_command(f"new dup --path {fresh}", FakeGx())
    assert "already registered" in out2
    assert not fresh.exists()             # register runs before mkdir → no orphan dir


def test_mint_requires_agent(mint_env):
    # without a session agent the mint can't run the switch → fail-closed
    out = gx10._project_command("new noagent")
    assert "requires an interactive session" in out
    assert not any(p.id == "noagent" for p in gx10._REGISTRY.list())
