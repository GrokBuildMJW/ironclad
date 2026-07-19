"""ACE-FORKPROOF (#855 / #886, M5-5) — the M5 capstone: a boundary-clean END-TO-END proof of the whole
MPR-for-architecture propose-loop through one shared seam for multiple ledger producers, inert when gated off.

Drives M5-1..M5-4 with stubs (deterministic, no network): a `ForkSignal` on the ledger → M5-2 fires the
gated, off-path, pre-informed MPR panel → M5-3 records + renders the decision-matrix as a recommendation →
the operator resolves (a `ForkResolution`) → M5-4 records a fork-decision bullet → a SECOND comparable
`ForkSignal`'s MPR query is pre-informed by that bullet. Also: producer parity through the same seam and
ledger schema, the gate-off no-op, and the fail-soft matrix. Reads the ledger as plain data and imports no
producer-specific implementation.
"""
from __future__ import annotations

import json
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
from ack.ace import ReflectionWorker, ForkSignal, FORK_SURFACE, FORK_RESOLVED_SURFACE
from playbook_store import PlaybookStore

_Q = "worktree isolation vs shared tree for parallel workers?"
_MATRIX = "## Decision matrix\n| Option | Score |\n|---|---|\n**Recommendation:** worktree isolation"
# a learned lesson whose content overlaps the fork question, so context_for retrieves it lexically
_LEARNED = "for worktree-vs-shared-tree parallel-worker forks, worktree isolation is the durable choice"


def _fork(unit, q=_Q):
    return {"surface": FORK_SURFACE, "unit": unit, "area": "engine", "question": q,
            "options": ["worktree isolation", "shared tree"]}


def _resolved(unit, chosen="worktree isolation", outcome="delivered"):
    return {"surface": FORK_RESOLVED_SURFACE, "unit": unit, "area": "engine",
            "chosen_option": chosen, "outcome": outcome}


def _chat(insight):
    return lambda p: json.dumps({"insights": [{"content": insight, "section": "strategies_and_hard_rules"}],
                                 "ratings": []})


def _hard_reset():
    for w in (gx10._ACE_WORKER, gx10._ACE_FORK_WORKER):
        try:
            w and w.stop()
        except Exception:
            pass
    gx10._ACE_WORKER = gx10._ACE_FORK_WORKER = gx10._ACE_STORE = None
    gx10._ACE_FORK_MPR = False


@pytest.fixture(autouse=True)
def _reset(tmp_path, monkeypatch):
    _hard_reset()
    monkeypatch.setattr(project_registry, "ironclad_home", lambda: tmp_path)
    monkeypatch.setattr(gx10, "_active_mem_ns", lambda default="": "ns", raising=False)
    yield
    _hard_reset()


def _setup(monkeypatch, tmp_path, *, gate=True, matrix=_MATRIX, learned=_LEARNED):
    store = gx10._ACE_STORE = PlaybookStore(tmp_path / "pb")
    store.set_transports(chat=_chat(learned))
    gx10._ACE_WORKER = ReflectionWorker(gx10._ace_run_task)
    gx10._ACE_FORK_WORKER = ReflectionWorker(gx10._ace_fork_run_task) if gate else None
    gx10._ACE_FORK_MPR = gate
    calls = []
    monkeypatch.setattr(gx10, "run_tool", lambda name, args: (calls.append((name, args)), matrix)[1])
    return store, calls


def _propose(ledger):
    gx10._ace_scan_fork_signals(ledger, [])
    if gx10._ACE_FORK_WORKER is not None:
        gx10._ACE_FORK_WORKER.process_pending()      # MPR panel → record_fork_proposal


def _learn(ledger):
    gx10._ace_scan_fork_resolutions(ledger, [])
    gx10._ACE_WORKER.process_pending()               # fork-decision trajectory → bullet


# ─── the full gated loop end-to-end: propose → decide → record → pre-inform ──────────────────────────
def _run_full_loop(store, calls, *, first_unit, second_unit):
    # 1. PROPOSE: a declared fork fires the MPR panel; the matrix is rendered as a recommendation
    _propose([_fork(first_unit)])
    assert calls and calls[0][0] == "mpr_research" and calls[0][1]["domain_hint"] == "architecture-decision"
    proposal = gx10._ace_fork_proposal_for(str(first_unit))
    assert "recommendation only" in proposal.lower() and "Decision matrix" in proposal
    # 2. DECIDE + RECORD: the operator resolves → a fork-decision bullet is learned
    _learn([_fork(first_unit), _resolved(first_unit)])
    assert any(_LEARNED in l for l in store.get_lessons("ns"))
    # 3. PRE-INFORM: a SECOND comparable fork's MPR query carries the learned decision
    q2 = gx10._ace_fork_query(ForkSignal(unit=str(second_unit), question=_Q,
                                         options=["worktree isolation", "shared tree"]), "ns")
    assert _LEARNED in q2 and "Prior comparable decisions" in q2


def test_full_loop_public_generic_devprocess(tmp_path, monkeypatch):
    store, calls = _setup(monkeypatch, tmp_path)
    _run_full_loop(store, calls, first_unit="701", second_unit="702")   # a ledger the public ack.devprocess wrote


def test_full_loop_internal_dev3_same_seam(tmp_path, monkeypatch):
    store, calls = _setup(monkeypatch, tmp_path)
    _run_full_loop(store, calls, first_unit="811", second_unit="812")   # the SAME schema the internal DEV-3 emits


# ─── gate OFF ⇒ byte-identical: no MPR, no proposal, no learning, no pre-informing ───────────────────
def test_gate_off_is_fully_inert(tmp_path, monkeypatch):
    store, calls = _setup(monkeypatch, tmp_path, gate=False)
    _propose([_fork("701")])
    gx10._ace_scan_fork_resolutions([_fork("701"), _resolved("701")], [])
    gx10._ACE_WORKER.process_pending()
    assert calls == []                                       # MPR never fired
    assert gx10._ace_fork_proposal_for("701") == ""          # no proposal attached to the ask
    assert store.get_lessons("ns") == []                     # nothing learned
    q = gx10._ace_fork_query(ForkSignal(unit="702", question=_Q), "ns")
    assert "Prior comparable decisions" not in q             # the next fork is not pre-informed


# ─── the fail-soft matrix: no crash, the ask always surfaces, the loop never blocks ──────────────────
def test_failsoft_matrix(tmp_path, monkeypatch):
    # tampered ledger ⇒ neither scan dispatches
    store, calls = _setup(monkeypatch, tmp_path)
    assert gx10._ace_scan_fork_signals([_fork("701")], ["hash mismatch"]) == 0
    assert gx10._ace_scan_fork_resolutions([_resolved("701")], ["hash mismatch"]) == 0
    assert calls == []
    # MPR returns a no-op (disabled / no initiative) ⇒ dispatched, but NO proposal recorded (ask unchanged)
    store2, _ = _setup(monkeypatch, tmp_path, matrix="ERROR: mpr_research: no active initiative")
    _propose([_fork("702")])
    assert gx10._ace_fork_proposal_for("702") == ""
    # MPR raises ⇒ the fork worker swallows it (no crash), the ask still surfaces
    def _boom(name, args): raise RuntimeError("mpr blew up")
    store3, _ = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(gx10, "run_tool", _boom)
    _propose([_fork("703")])
    assert gx10._ACE_FORK_WORKER.errors == 0 and gx10._ace_fork_proposal_for("703") == ""
    # a malformed fork signal ⇒ skipped, never raises
    assert gx10._ace_scan_fork_signals([None, {}, "x", {"surface": FORK_SURFACE}], []) == 0
