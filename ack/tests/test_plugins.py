"""SE-2: the open plugin surface — a skill becomes an agent tool (gx10._load_plugins).

A plugin is a `.py` under a `skills/` dir with a module `CASE` dict (name/description/
capability) and a `run(...)` function. The engine discovers it from the configured plugins
dir and offers it as a tool; calling the tool dispatches to `run`. The core is untouched.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=object))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402
import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_plugins():
    yield
    gx10._PLUGIN_TOOLS.clear()


def _plugin_dir(tmp_path: Path, filename: str, body: str) -> str:
    skills = tmp_path / "skills"
    skills.mkdir(parents=True, exist_ok=True)
    (skills / filename).write_text(body, encoding="utf-8")
    return str(tmp_path)


_GREET = (
    'CASE = {"name": "greet", "description": "Greet someone by name", '
    '"capability": "greet"}\n'
    "def run(name: str) -> str:\n"
    '    return f"Hello, {name}!"\n'
)


def test_plugin_is_discovered_and_offered(tmp_path):
    assert gx10._load_plugins(_plugin_dir(tmp_path, "greet.py", _GREET)) == 1
    assert "greet" in gx10._PLUGIN_TOOLS
    names = [t["function"]["name"] for t in gx10._effective_tools()]
    assert "greet" in names
    # the parameter schema is derived from run()'s signature
    params = gx10._PLUGIN_TOOLS["greet"]["schema"]["function"]["parameters"]
    assert "name" in params["properties"] and params["properties"]["name"]["type"] == "string"


def test_plugin_colliding_with_a_builtin_is_skipped(tmp_path):
    # ROUTE-4 (#503): a plugin named like a BUILT-IN tool is shadowed by run_tool's built-in dispatch →
    # it would be registered + offered but NEVER callable (silently). It must be rejected at load.
    builtins = gx10._all_tool_names(include_plugins=False)
    assert "read_file" in builtins                      # sanity: read_file is a built-in tool
    body = ('CASE = {"name": "read_file", "description": "shadow", "capability": "shadow-read"}\n'
            "def run(path: str) -> str:\n    return path\n")
    assert gx10._load_plugins(_plugin_dir(tmp_path, "shadow.py", body)) == 0   # skipped, not offered
    assert "read_file" not in gx10._PLUGIN_TOOLS


def test_b22_routing_regressions():
    # ROUTE-1: post-advance regen is config-gated (default empty ⇒ no hardcoded subprocess in core)
    assert gx10._code_defaults()["paths"]["post_advance_hooks"] == []
    # ROUTE-3: parallel_reason now exposes `effort` in its schema (was read but omitted → pinned to medium)
    assert "effort" in gx10.PARALLEL_TOOL["function"]["parameters"]["properties"]
    # ROUTE-2: the dead _TURN_DID_ADVANCE guard (only reset, never set/read) is gone
    assert not hasattr(gx10, "_TURN_DID_ADVANCE")
    # DEAD-APPLYCLI: the uncalled level-4 CLI override is gone
    assert not hasattr(gx10, "_apply_cli")


def test_duplicate_tool_name_keeps_first(tmp_path):
    # two skills with DISTINCT capabilities but the SAME tool name → the name must stay unique
    # (one registered tool, not a silent overwrite). Audit #28.
    skills = tmp_path / "skills"
    skills.mkdir(parents=True, exist_ok=True)
    (skills / "a.py").write_text(
        'CASE = {"name": "dup", "capability": "capa", "description": "A"}\n'
        "def run(x: str) -> str:\n    return 'A:' + x\n", encoding="utf-8")
    (skills / "b.py").write_text(
        'CASE = {"name": "dup", "capability": "capb", "description": "B"}\n'
        "def run(x: str) -> str:\n    return 'B:' + x\n", encoding="utf-8")
    n = gx10._load_plugins(str(tmp_path))
    assert n == 1                          # collision resolved: exactly one tool registered
    assert "dup" in gx10._PLUGIN_TOOLS


def test_plugin_tool_dispatches_to_run(tmp_path):
    gx10._load_plugins(_plugin_dir(tmp_path, "greet.py", _GREET))
    assert gx10.run_tool("greet", {"name": "Ada"}) == "Hello, Ada!"


def test_no_plugins_dir_is_empty():
    assert gx10._load_plugins(None) == 0
    assert gx10._load_plugins("") == 0
    assert gx10._PLUGIN_TOOLS == {}


def test_broken_plugin_is_skipped_not_fatal(tmp_path):
    # a skill that raises at import time must be skipped, not crash discovery
    n = gx10._load_plugins(_plugin_dir(tmp_path, "bad.py", "raise RuntimeError('boom')\n"))
    assert n == 0 and gx10._PLUGIN_TOOLS == {}


def test_async_run_is_rejected_cleanly(tmp_path):
    body = ('CASE = {"name": "slow", "capability": "slow"}\n'
            "async def run():\n    return 'x'\n")
    gx10._load_plugins(_plugin_dir(tmp_path, "slow.py", body))
    out = gx10.run_tool("slow", {})
    assert "async" in out and out.startswith("ERROR")


def test_load_clears_previous(tmp_path):
    gx10._load_plugins(_plugin_dir(tmp_path, "greet.py", _GREET))
    assert "greet" in gx10._PLUGIN_TOOLS
    gx10._load_plugins(None)                 # reloading with nothing clears the set
    assert gx10._PLUGIN_TOOLS == {}
