"""The shipped example plugin (#138) proves the separate-repo authoring shape: a standalone
package with a `skills/` dir + an `ironclad.plugins` entry point, built against the SDK. Here we
verify it discovers + runs through the engine loader and matches the `ack.sdk` schema contract.

The example lives under `examples/` (export root), not inside the `ack` package, so it is
absent from an installed/clean-room tree — the test **skips** there (the clean-room workflow
proves the installed/entry-point path instead).
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
from ack import sdk  # noqa: E402

_PKG = Path(__file__).resolve().parents[2] / "examples" / "example-plugin" / "ironclad_example_plugin"

pytestmark = pytest.mark.skipif(
    not (_PKG / "skills" / "reverse.py").is_file(),
    reason="example plugin absent — installed/clean-room tree (covered by the clean-room workflow)",
)


@pytest.fixture(autouse=True)
def _clean():
    yield
    gx10._PLUGIN_TOOLS.clear()
    gx10._PLAYBOOKS.clear()
    gx10._PROMPTS.clear()


def test_example_discovers_and_runs_through_the_loader():
    gx10._discover_tools_into(str(_PKG))          # scan the package's skills/ dir
    assert "reverse" in gx10._PLUGIN_TOOLS
    handler = gx10._PLUGIN_TOOLS["reverse"]["handler"]
    assert handler(text="hello") == "olleh"


def test_example_schema_matches_sdk_derivation():
    import importlib.util as u
    spec = u.spec_from_file_location("_ex_reverse", _PKG / "skills" / "reverse.py")
    mod = u.module_from_spec(spec)
    spec.loader.exec_module(mod)
    schema = sdk.derive_tool_schema(mod.run)
    assert schema["type"] == "object"
    assert "text" in schema["properties"] and "text" in schema["required"]


def test_resolve_plugin_root_finds_the_example_package():
    pkg = types.SimpleNamespace(__path__=[str(_PKG)])     # what an entry point resolves to
    root = gx10._resolve_plugin_root(pkg)
    assert root == str(_PKG)
    gx10._discover_tools_into(root)
    assert "reverse" in gx10._PLUGIN_TOOLS                # entry-point path → same discovery


def test_example_passes_the_sdk_gate():
    # the example ships its sibling tests/test_reverse.py, so it passes the same gate Ironclad runs
    # before trusting a skill — the example must not fail its own documented validate step (#260)
    res = sdk.gate(str(_PKG / "skills" / "reverse.py"))
    assert res, f"gate failed: {res.reasons}"
    assert res.kind == "tool"
