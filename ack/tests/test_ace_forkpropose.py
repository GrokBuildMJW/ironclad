"""ACE-FORKPROPOSE (#855 / #884, M5-3): the propose surface — the MPR decision-matrix (produced off-path by
M5-2) is recorded as a fork proposal bound to the unit and rendered to the operator ask as a RECOMMENDATION
only (MPR-A-3). The operator still decides; M5 never auto-commits. Fail-soft: a no-op MPR result / no matrix ⇒
the ask surfaces unchanged (no empty artifact). Deterministic — a stub run_tool, no MPR/network.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=lambda **kw: object()))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import project_registry
import gx10
from ack.ace import ForkSignal
from playbook_store import read_fork_proposal

_MATRIX = ("## Decision matrix\n\n| Option | Score |\n|---|---|\n| worktree isolation | 9 |\n"
           "| shared tree | 4 |\n\n**Recommendation:** worktree isolation (best isolation, low cost)\n"
           "Dissent: the perf lens prefers shared tree for small changes.")


@pytest.fixture(autouse=True)
def _reset(tmp_path, monkeypatch):
    monkeypatch.setattr(project_registry, "ironclad_home", lambda: tmp_path)
    monkeypatch.setattr(gx10, "_active_mem_ns", lambda default="": "ns", raising=False)
    gx10._ACE_STORE = None
    yield


def _sig(unit="880", q="worktree vs shared tree?"):
    return ForkSignal(unit=unit, area="engine", question=q, options=["worktree isolation", "shared tree"])


# ─── the produced matrix is recorded as a proposal bound to the unit ─────────────────────────────────
def test_fork_mpr_run_persists_matrix_as_proposal(tmp_path, monkeypatch):
    monkeypatch.setattr(gx10, "run_tool", lambda name, args: _MATRIX)
    out = gx10._ace_fork_mpr_run(_sig(unit="880"), "ns")
    assert out == _MATRIX
    assert read_fork_proposal(tmp_path, "880") == _MATRIX      # bound to the unit for the ask surface


def test_noop_mpr_result_is_not_a_proposal(tmp_path, monkeypatch):
    for noop in ("MPR is disabled (mpr.enabled off)", "ERROR: mpr_research: no active initiative", "MPR declined"):
        monkeypatch.setattr(gx10, "run_tool", lambda name, args, r=noop: r)
        gx10._ace_fork_mpr_run(_sig(unit="881"), "ns")
        assert read_fork_proposal(tmp_path, "881") == ""       # fail-soft: the ask surfaces unchanged, no artifact


# ─── the render seam is RECOMMENDATION-only ──────────────────────────────────────────────────────────
def test_proposal_render_is_recommendation_only(tmp_path, monkeypatch):
    monkeypatch.setattr(gx10, "run_tool", lambda name, args: _MATRIX)
    gx10._ace_fork_mpr_run(_sig(unit="882"), "ns")
    rendered = gx10._ace_fork_proposal_for("882")
    assert "recommendation only (you decide)" in rendered.lower()
    assert "**MPR's top-ranked option:** worktree isolation" in rendered   # extracted top-ranked option
    assert "| worktree isolation | 9 |" in rendered            # the full matrix (ranked options + dissent)
    assert "NOT a decision" in rendered and "ACE learns" in rendered       # framed as a recommendation


def test_no_proposal_renders_empty(tmp_path):
    assert gx10._ace_fork_proposal_for("no-such-unit") == ""   # no matrix ⇒ ask unchanged
    assert gx10._ace_fork_proposal_for("") == ""


# ─── the top-ranked extraction is robust across synthesis phrasings ──────────────────────────────────
def test_extract_recommendation_variants():
    assert gx10._ace_extract_recommendation("**Recommendation:** option A") == "option A"
    assert gx10._ace_extract_recommendation("- Verdict: go with B") == "go with B"
    assert gx10._ace_extract_recommendation("## Recommended — the event seam") == "the event seam"
    assert gx10._ace_extract_recommendation("no verdict line here\njust prose") == ""   # none ⇒ matrix speaks
    assert gx10._ace_extract_recommendation("") == ""


# ─── the /fork operator surface: the production caller of the M5-3 output leg (C2 #903) ──────────────
def test_fork_command_renders_and_lists(tmp_path, monkeypatch):
    monkeypatch.setattr(gx10, "run_tool", lambda name, args: _MATRIX)
    assert "No pending MPR fork proposals" in gx10._fork_command("")     # nothing recorded yet
    gx10._ace_fork_mpr_run(_sig(unit="880"), "ns")                       # M5-2 records a proposal for #880
    out = gx10._fork_command("")                                         # single pending → rendered
    assert "recommendation only" in out.lower() and "worktree isolation" in out
    assert "recommendation only" in gx10._fork_command("880").lower()    # /fork 880 (and #880 form)
    assert "recommendation only" in gx10._fork_command("#880").lower()
    assert "No MPR fork proposal recorded for #999" in gx10._fork_command("999")


def test_fork_command_lists_multiple(tmp_path, monkeypatch):
    monkeypatch.setattr(gx10, "run_tool", lambda name, args: _MATRIX)
    gx10._ace_fork_mpr_run(_sig(unit="701"), "ns")
    gx10._ace_fork_mpr_run(_sig(unit="702"), "ns")
    out = gx10._fork_command("")
    assert "2 pending" in out and "#701" in out and "#702" in out       # list + hint, not a wall of matrices
