"""ACE-FORKLEARN (#855 / #885, M5-4): the learn leg for forks — an operator's fork decision + outcome
(M5-1's ForkResolution on the ledger) becomes a fork-decision Trajectory submitted to the existing
ReflectionWorker → reflect→curate writes a bullet, keyed by the fork's question so M5-2's `context_for`
pre-informs the next comparable fork. Closes the propose→decide→record→pre-inform loop. Gate OFF ⇒ no
learning (byte-identical); exactly-once; distinct from M4-2/#863; fail-soft. Deterministic (fake chat).
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
from ack.ace import ReflectionWorker, FORK_SURFACE, FORK_RESOLVED_SURFACE
from playbook_store import PlaybookStore, record_unit_bullets


class _RecWorker:
    def __init__(self): self.items = []
    def submit(self, item): self.items.append(item); return True
    def process_pending(self): return 0


def _resolved(unit="880", area="engine", chosen="worktree isolation", outcome="delivered"):
    return {"surface": FORK_RESOLVED_SURFACE, "unit": unit, "area": area,
            "chosen_option": chosen, "outcome": outcome}


def _fork(unit="880", q="worktree isolation vs shared tree for parallel workers?"):
    return {"surface": FORK_SURFACE, "unit": unit, "area": "engine", "question": q,
            "options": ["worktree isolation", "shared tree"]}


def _chat(insight):
    return lambda p: json.dumps({"insights": [{"content": insight, "section": "strategies_and_hard_rules"}],
                                 "ratings": []})


def _hard_reset():
    for w in (gx10._ACE_WORKER, gx10._ACE_FORK_WORKER):
        try:
            w and w.stop()
        except Exception:
            pass
    gx10._ACE_WORKER = None
    gx10._ACE_FORK_WORKER = None
    gx10._ACE_STORE = None
    gx10._ACE_FORK_MPR = False


@pytest.fixture(autouse=True)
def _reset(tmp_path, monkeypatch):
    _hard_reset()
    monkeypatch.setattr(project_registry, "ironclad_home", lambda: tmp_path)
    monkeypatch.setattr(gx10, "_active_mem_ns", lambda default="": "ns", raising=False)
    yield
    _hard_reset()


def _setup(tmp_path, monkeypatch, chat, *, gate=True, worker=None):
    store = gx10._ACE_STORE = PlaybookStore(tmp_path / "pb")
    if chat is not None:
        store.set_transports(chat=chat)
    gx10._ACE_WORKER = worker if worker is not None else ReflectionWorker(gx10._ace_run_task)
    gx10._ACE_FORK_MPR = gate
    return store


def _drive(payloads, chain_errors=None):
    n = gx10._ace_scan_fork_resolutions(payloads, chain_errors if chain_errors is not None else [])
    gx10._ACE_WORKER.process_pending()
    return n


# ─── gate OFF ⇒ no learning (byte-identical) ─────────────────────────────────────────────────────────
def test_gate_off_no_learning(tmp_path, monkeypatch):
    store = _setup(tmp_path, monkeypatch, _chat("x"), gate=False)
    assert _drive([_fork(), _resolved()]) == 0
    assert store.get_lessons("ns") == []


# ─── a resolved fork learns a bullet, retrievable by a comparable question (pre-informs the next fork) ──
def test_resolved_fork_learns_retrievable_bullet(tmp_path, monkeypatch):
    store = _setup(tmp_path, monkeypatch,
                   _chat("for parallel-worker isolation forks, worktree isolation is the durable default"))
    assert _drive([_fork(), _resolved()]) == 1
    lessons = store.get_lessons("ns")
    assert any("worktree isolation is the durable default" in l for l in lessons)   # learned
    # M5-2 pre-informing: the next comparable fork's context_for surfaces it
    ctx = store.context_for(["ns"], query="worktree isolation vs shared tree for parallel workers?")
    assert "worktree isolation is the durable default" in ctx


# ─── the fork-decision trajectory shape (query = comparability key; used_bullet_ids from the seed map) ──
def test_fork_trajectory_shape_and_used_bullets(tmp_path, monkeypatch):
    rec = _RecWorker()
    _setup(tmp_path, monkeypatch, None, worker=rec)
    record_unit_bullets(tmp_path, "fork:880", ["b-3", "b-7"])     # M5-4 capture: bullets that seeded the query
    assert gx10._ace_scan_fork_resolutions([_fork(), _resolved(unit="880")]) == 1
    traj = rec.items[0]["trajectory"]
    assert traj.query == "architecture fork: worktree isolation vs shared tree for parallel workers?"
    assert "chose 'worktree isolation'" in traj.outcome and "delivered" in traj.outcome
    assert traj.used_bullet_ids == ["b-3", "b-7"]                 # the Reflector can rate which prior helped


def test_trajectory_none_on_empty_resolution():
    assert gx10._ace_fork_trajectory({}, "", []) is None
    assert gx10._ace_fork_trajectory(type("R", (), {"chosen_option": "x", "area": ""})(), "", []) is None


# ─── exactly-once + double-learning guard + fail-soft ────────────────────────────────────────────────
def test_exactly_once_per_decision(tmp_path, monkeypatch):
    rec = _RecWorker()
    _setup(tmp_path, monkeypatch, None, worker=rec)
    ledger = [_resolved(unit="701", chosen="A")]
    assert gx10._ace_scan_fork_resolutions(ledger) == 1
    assert gx10._ace_scan_fork_resolutions(ledger) == 0          # same decision → not re-learned
    assert len(rec.items) == 1


def test_failsoft_tampered_and_empty(tmp_path, monkeypatch):
    rec = _RecWorker()
    _setup(tmp_path, monkeypatch, None, worker=rec)
    assert gx10._ace_scan_fork_resolutions([_resolved()], ["hash mismatch"]) == 0   # tampered → skip
    assert gx10._ace_scan_fork_resolutions([None, {}, "x", _fork()]) == 0           # no resolution → 0
    assert rec.items == []
