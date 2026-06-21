"""Entry-point plugin loading seam (ADR-0004 #136): a *packaged* plugin advertised via the
`ironclad.plugins` entry-point group is discovered through the contract — the engine never imports
a concrete plugin (dependency inversion). Covers root resolution (dir/package/module/callable),
additive loading alongside built-ins + `GX10_PLUGINS_DIR`, and fail-soft on a broken entry point.
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
def _clean():
    yield
    gx10._PLUGIN_TOOLS.clear()
    gx10._PLAYBOOKS.clear()
    gx10._PROMPTS.clear()


class _FakeEP:
    def __init__(self, name, target):
        self.name = name
        self._target = target

    def load(self):
        return self._target


def _tool_dir(root: Path, cap: str) -> None:
    skillgen.write_scaffold(skillgen.SkillSpec(capability=cap, description="d", kind="tool",
                                               params=[("x", "str")]), root, force=True)


# ── _resolve_plugin_root ──────────────────────────────────────────────
def test_resolve_root_from_dir_path(tmp_path):
    assert gx10._resolve_plugin_root(str(tmp_path)) == str(tmp_path)
    assert gx10._resolve_plugin_root(tmp_path) == str(tmp_path)


def test_resolve_root_from_missing_dir_is_none(tmp_path):
    assert gx10._resolve_plugin_root(str(tmp_path / "nope")) is None


def test_resolve_root_from_callable(tmp_path):
    assert gx10._resolve_plugin_root(lambda: str(tmp_path)) == str(tmp_path)


def test_resolve_root_from_package_like(tmp_path):
    pkg = types.SimpleNamespace(__path__=[str(tmp_path)])
    assert gx10._resolve_plugin_root(pkg) == str(tmp_path)


def test_resolve_root_from_module_like(tmp_path):
    mod = types.SimpleNamespace(__file__=str(tmp_path / "__init__.py"))
    assert gx10._resolve_plugin_root(mod) == str(tmp_path)


# ── _entrypoint_plugin_roots + _load_skills ───────────────────────────
def test_load_skills_discovers_entrypoint_plugin(tmp_path, monkeypatch):
    builtin = tmp_path / "builtin"; ep_plugin = tmp_path / "ep"
    _tool_dir(builtin, "core-tool")
    _tool_dir(ep_plugin, "ep-tool")
    monkeypatch.setattr(gx10, "_BUILTIN_DIR", builtin)
    monkeypatch.setattr(gx10, "_iter_plugin_entry_points",
                        lambda: [_FakeEP("ep", str(ep_plugin))])
    n_tools, _, _ = gx10._load_skills(None)
    assert "core-tool" in gx10._PLUGIN_TOOLS    # built-in
    assert "ep-tool" in gx10._PLUGIN_TOOLS      # discovered via entry point, no dir config
    assert n_tools >= 2


def test_entrypoint_is_additive_to_dir_plugins(tmp_path, monkeypatch):
    builtin = tmp_path / "builtin"; plug = tmp_path / "plug"; ep_plugin = tmp_path / "ep"
    _tool_dir(builtin, "core-tool")
    _tool_dir(plug, "dir-tool")
    _tool_dir(ep_plugin, "ep-tool")
    monkeypatch.setattr(gx10, "_BUILTIN_DIR", builtin)
    monkeypatch.setattr(gx10, "_iter_plugin_entry_points",
                        lambda: [_FakeEP("ep", str(ep_plugin))])
    gx10._load_skills(str(plug))
    assert {"core-tool", "dir-tool", "ep-tool"} <= set(gx10._PLUGIN_TOOLS)


def test_broken_entry_point_is_fail_soft(tmp_path, monkeypatch):
    builtin = tmp_path / "builtin"
    _tool_dir(builtin, "core-tool")

    class _BadEP:
        name = "bad"
        def load(self):
            raise RuntimeError("boom")

    monkeypatch.setattr(gx10, "_BUILTIN_DIR", builtin)
    monkeypatch.setattr(gx10, "_iter_plugin_entry_points", lambda: [_BadEP()])
    n_tools, _, _ = gx10._load_skills(None)      # must not raise
    assert "core-tool" in gx10._PLUGIN_TOOLS
    assert n_tools >= 1


def test_no_entry_points_means_only_builtins(tmp_path, monkeypatch):
    builtin = tmp_path / "builtin"
    _tool_dir(builtin, "core-tool")
    monkeypatch.setattr(gx10, "_BUILTIN_DIR", builtin)
    monkeypatch.setattr(gx10, "_iter_plugin_entry_points", lambda: [])
    gx10._load_skills(None)
    assert "core-tool" in gx10._PLUGIN_TOOLS


def test_iter_entry_points_is_callable_and_safe():
    # the real implementation must not raise in an environment with no such group
    assert isinstance(gx10._iter_plugin_entry_points(), list)
