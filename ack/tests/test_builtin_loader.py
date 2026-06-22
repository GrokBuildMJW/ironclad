"""Always-on core built-in loader (ADR-0002 #114): built-ins load from a fixed core dir at
startup independent of GX10_PLUGINS_DIR; the plugin surface stays additive for 3rd-party.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=object))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402

from ack import skillgen  # noqa: E402


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    # isolate from any ambient installed `ironclad.plugins` entry point (e.g. a locally-built example
    # whose egg-info lands on sys.path): these tests assert built-in / explicit-dir loading, not what
    # happens to be pip-installed in the dev env. Mirrors test_entrypoint_loader.py.
    monkeypatch.setattr(gx10, "_iter_plugin_entry_points", lambda: [])
    yield
    gx10._PLUGIN_TOOLS.clear()
    gx10._PLAYBOOKS.clear()
    gx10._PROMPTS.clear()


def _tool(root: Path, cap: str):
    skillgen.write_scaffold(skillgen.SkillSpec(capability=cap, description="d", kind="tool",
                                               params=[("x", "str")]), root, force=True)


def _playbook(root: Path, cap: str):
    skillgen.write_scaffold(skillgen.SkillSpec(capability=cap, description="d", kind="playbook",
                                               trigger=["go"]), root, force=True)


def test_builtins_load_without_plugins_dir(tmp_path, monkeypatch):
    builtin = tmp_path / "builtin"
    _tool(builtin, "core-tool")
    _playbook(builtin, "core-pb")
    monkeypatch.setattr(gx10, "_BUILTIN_DIR", builtin)
    n_tools, n_pb, n_prompts = gx10._load_skills(None)   # no plugins_dir at all
    assert n_tools >= 1 and n_pb >= 1
    assert "core-tool" in gx10._PLUGIN_TOOLS
    assert "core-pb" in gx10._PLAYBOOKS
    names = {t["function"]["name"] for t in gx10._effective_tools()}
    assert "core-tool" in names and "use_skill" in names   # built-in tool + playbook surface


def test_plugins_dir_is_additive_to_builtins(tmp_path, monkeypatch):
    builtin = tmp_path / "builtin"; plug = tmp_path / "plug"
    _tool(builtin, "core-tool")
    _tool(plug, "ext-tool")
    monkeypatch.setattr(gx10, "_BUILTIN_DIR", builtin)
    gx10._load_skills(str(plug))
    assert "core-tool" in gx10._PLUGIN_TOOLS    # built-in
    assert "ext-tool" in gx10._PLUGIN_TOOLS     # 3rd-party, additive


def test_empty_builtin_dir_is_fine(tmp_path, monkeypatch):
    monkeypatch.setattr(gx10, "_BUILTIN_DIR", tmp_path / "nonexistent")
    n_tools, n_pb, n_prompts = gx10._load_skills(None)
    assert n_tools == 0 and n_pb == 0 and n_prompts == 0   # fail-soft, no crash


def test_single_dir_loaders_still_work(tmp_path):
    # back-compat: _load_plugins/_load_playbooks load exactly their dir (clear first)
    d = tmp_path / "d"
    _tool(d, "solo-tool")
    _playbook(d, "solo-pb")
    assert gx10._load_plugins(str(d)) == 1 and "solo-tool" in gx10._PLUGIN_TOOLS
    assert gx10._load_playbooks(str(d)) == 1 and "solo-pb" in gx10._PLAYBOOKS
