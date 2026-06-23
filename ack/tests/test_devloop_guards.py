"""Machine-gated dev-loop structured guards (epic #262, S2 / ADR 0002), offline.

Lives in `scripts/devloop/` (private) -> skips in an installed/clean-room tree. Pins the guard
framework: exit-code wrapping is **fail-closed** (a missing binary is RED, not a silent pass),
`compose` aggregates a profile correctly, `english_only` flags German characters, and
`gate_profile_commands` composes only the target-agnostic guards (core/-export-only guards are
phase-2). Each behaviour has its positive AND its negative case (ADR-0007 discipline).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_GUARDS = _REPO / "scripts" / "devloop" / "guards.py"

pytestmark = pytest.mark.skipif(
    not _GUARDS.is_file(),
    reason="private dev-loop guards (scripts/devloop/guards.py) absent — installed/clean-room tree",
)


def _load():
    spec = importlib.util.spec_from_file_location("_devloop_guards", _GUARDS)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_shell_guard_pass_and_fail(tmp_path):
    g = _load()
    assert g.shell_guard("ok", [sys.executable, "-c", "import sys; sys.exit(0)"], tmp_path)
    red = g.shell_guard("bad", [sys.executable, "-c", "import sys; sys.exit(1)"], tmp_path)
    assert not red and red.reasons


def test_shell_guard_is_fail_closed_on_missing_binary(tmp_path):
    g = _load()
    r = g.shell_guard("nope", ["definitely-not-a-real-binary-xyz123"], tmp_path)
    assert not r.passed                                  # an unrunnable gate is RED, never a pass
    assert any("fail-closed" in x for x in r.reasons)


def test_compose_all_green_vs_any_red():
    g = _load()
    ok = [g.GuardResult("a", True), g.GuardResult("b", True)]
    assert g.compose("profile", ok).passed
    mixed = [g.GuardResult("a", True), g.GuardResult("b", False, ["b broke"])]
    out = g.compose("profile", mixed)
    assert not out.passed and "b broke" in out.reasons


def test_english_only_flags_german_and_passes_clean(tmp_path):
    g = _load()
    en = tmp_path / "ok.py"; en.write_text("def run(text):\n    return text.lower()\n", encoding="utf-8")
    de = tmp_path / "bad.py"; de.write_text("# zaehle die Fuesse\nGROESSE = 1  # Fuelldaten: Groesse\n".replace("ue", "ü").replace("oe", "ö"), encoding="utf-8")
    assert g.english_only("en", [en]).passed
    red = g.english_only("de", [de])
    assert not red.passed and "english-only" in red.reasons[0]


def test_gate_profile_composes_only_target_agnostic_guards():
    g = _load()
    plugin = {"boundary_cmd": "python scripts/check_plugin_boundary.py .",
              "gate_profile": ["boundary", "pytest"]}
    core = {"boundary_cmd": "python scripts/ci/check_core_boundary.py",
            "gate_profile": ["boundary", "pytest", "doc-reality-audit", "test-counts", "node-boundary", "english-only"]}
    pc = g.gate_profile_commands(plugin, ".")
    cc = g.gate_profile_commands(core, ".")
    assert set(pc) == {"boundary", "pytest"}
    assert set(cc) == {"boundary", "pytest"}             # core-only guards are NOT composed (phase-2)
    assert pc["boundary"][0] == sys.executable and "check_plugin_boundary.py" in pc["boundary"][1]
