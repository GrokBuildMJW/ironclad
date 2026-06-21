"""Merge-Gate runner (Spec 08 §7) — pure fail-fast logic + stage inventory. The runner lives in
scripts/ci/ (subprocess is banned inside skills/mpr/ by the §2.4 AST guard), so it is loaded by path."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

# Layout-agnostic: the private monorepo nests the plugin under core/ (skills/mpr/tests →
# repo root is parents[4]); a flat tree has parents[3]. The gate runner is a PRIVATE CI script
# (scripts/ci/, not shipped in the public export) → skip cleanly when it isn't present.
_here = Path(__file__).resolve()
_GATE = next((c for c in (_here.parents[4] / "scripts" / "ci" / "mpr_gate.py",
                          _here.parents[3] / "scripts" / "ci" / "mpr_gate.py") if c.is_file()), None)
if _GATE is None:
    pytest.skip("mpr_gate.py (private CI script) not present in this tree", allow_module_level=True)


def _load():
    spec = importlib.util.spec_from_file_location("mpr_gate_probe", _GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = _load()


def test_run_stages_passes_when_all_zero():
    calls = []
    passed, results = M.run_stages(M.STAGES, run=lambda argv: calls.append(argv) or 0)
    assert passed is True
    assert [n for n, _ in results] == [s["name"] for s in M.STAGES]   # every stage attempted
    assert len(calls) == len(M.STAGES)


def test_run_stages_fail_fast_stops_at_first_failure():
    codes = iter([0, 1, 0, 0])                                        # stage 2 fails
    seen = []

    def _run(argv):
        seen.append(argv)
        return next(codes)
    passed, results = M.run_stages(M.STAGES, run=_run)
    assert passed is False
    assert [n for n, _ in results] == ["boundary", "suite"]           # stopped at the failing stage
    assert len(seen) == 2                                             # later stages never run (fail-fast)


def test_deterministic_stages_cover_section_7_and_are_offline():
    names = {s["name"] for s in M.STAGES}
    assert {"boundary", "suite", "harness-selftest", "judge-selftest"} <= names
    for s in M.STAGES:                                                # every default stage is real argv…
        assert s["argv"] and all(isinstance(a, str) for a in s["argv"])
        assert "--live" not in s["argv"]                             # …and none is a live/operator stage
    assert any("check_core_boundary.py" in a for s in M.STAGES for a in s["argv"])
    assert any("pytest" in a for s in M.STAGES for a in s["argv"])
    assert any("--selftest" in a for s in M.STAGES for a in s["argv"])


def test_live_stages_are_documented_not_runnable():
    # §7 stufe 4-5 are operator-run: documented (name+doc), never an auto-executed argv in STAGES.
    live = {s["name"] for s in M.LIVE_STAGES}
    assert {"ab-report", "live-smoke"} <= live
    assert all("doc" in s and "argv" not in s for s in M.LIVE_STAGES)
    assert live.isdisjoint({s["name"] for s in M.STAGES})            # live ≠ deterministic
