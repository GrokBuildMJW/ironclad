"""STATE-Layout Unit B3b: MPR runs route to the ACTIVE initiative.

An MPR run produces artifacts (runs/<id>/perspectives + synthesis + manifest) → they are initiative
artifacts and must land under vault/<slug>/runs, not the project root. The engine glue (_engine_deps)
binds runs_dir to the active initiative; the public entry (mpr_research_run) is fail-closed when no
initiative is active (it would otherwise write into the root).
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

# gx10 (imported lazily by _engine_deps / mpr_research_run) pulls in openai — stub it like the ack suite.
sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=lambda **kw: object()))

import gx10  # noqa: E402  (core/engine on sys.path via conftest)
from mpr.entry import _engine_deps, mpr_research_run  # noqa: E402


@pytest.fixture(autouse=True)
def _in_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_engine_deps_routes_runs_dir_to_active_initiative(tmp_path):
    gx10.initiative_new("Risk Review", "mpr")
    deps = _engine_deps()
    # runs land under vault/<slug>/runs (workdir-relative, posix)
    assert deps.runs_dir.replace("\\", "/").endswith("vault/risk-review/runs")
    assert "runs/mpr" not in deps.runs_dir          # not the old WORKDIR default


def test_runs_dir_falls_back_to_default_without_initiative(tmp_path):
    # no initiative active → the config default stands (the run itself is gated separately)
    deps = _engine_deps()
    assert deps.runs_dir == "runs/mpr"


def test_mpr_research_run_failclosed_without_initiative(tmp_path):
    out = mpr_research_run("Soll X auf Postgres laufen?")
    assert out.startswith("ERROR")
    assert "kein aktives Initiative" in out
    # nothing was written into the project root
    assert not (tmp_path / "runs").exists()


def test_mpr_research_run_passes_gate_with_initiative(tmp_path):
    # with a initiative active the fail-closed gate is cleared; the run then proceeds into the
    # orchestration (which, with no live LLM bound, returns its own router/degrade string — the point
    # here is only that it is NOT the "kein aktives Initiative" refusal).
    gx10.initiative_new("Decide", "mpr")
    out = mpr_research_run("Soll X auf Postgres laufen?")
    assert "kein aktives Initiative" not in out
