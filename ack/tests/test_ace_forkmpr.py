"""ACE-FORKMPR (#855 / #883, M5-2): the gated, off-hot-path MPR architecture-decision panel at a declared
fork (MPR-A-2) with the ACE-pre-informed query (MPR-A-5). Gate OFF ⇒ byte-identical no-op; gate ON ⇒ a
recognized ForkSignal fires `run_tool('mpr_research', domain_hint='architecture-decision', mode_hint=
'decision')` on the fork worker, seeded with the playbook's prior fork bullets; exactly-once; fail-soft.
Deterministic — a stub run_tool + stub store, no MPR/network.
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
from ack.ace import ReflectionWorker, FORK_SURFACE


class _Store:
    """A stub PlaybookStore exposing only the query-aware context_for (the pre-informing surface)."""
    def __init__(self, prior=""): self._prior = prior
    def context_for(self, scopes, query=""): return self._prior


def _fork(unit="880", q="ledger-derived or event seam?", opts=("ledger-derived", "event-seam")):
    return {"surface": FORK_SURFACE, "unit": unit, "area": "engine", "question": q, "options": list(opts)}


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
    gx10._ACE_FORK_INFLIGHT.clear()


@pytest.fixture(autouse=True)
def _reset(tmp_path, monkeypatch):
    _hard_reset()
    monkeypatch.setattr(project_registry, "ironclad_home", lambda: tmp_path)
    monkeypatch.setattr(gx10, "_active_mem_ns", lambda default="": "ns", raising=False)
    yield
    _hard_reset()


def _arm(monkeypatch, *, gate=True, store=None, tool=None):
    """Wire the gate + a real fork worker + a recording stub run_tool. Returns the calls list."""
    calls = []
    def _tool(name, args):
        calls.append((name, args))
        return tool(name, args) if tool else "## Decision matrix\n..."
    monkeypatch.setattr(gx10, "run_tool", _tool)
    gx10._ACE_STORE = store
    gx10._ACE_FORK_MPR = gate
    gx10._ACE_FORK_WORKER = ReflectionWorker(gx10._ace_fork_run_task) if gate else None
    return calls


def _drive(payloads, chain_errors=None):
    n = gx10._ace_scan_fork_signals(payloads, chain_errors if chain_errors is not None else [])
    if gx10._ACE_FORK_WORKER is not None:
        gx10._ACE_FORK_WORKER.process_pending()      # drain the fork MPR run synchronously
    return n


# ─── gate OFF ⇒ byte-identical no-op ─────────────────────────────────────────────────────────────────
def test_gate_off_is_noop_mpr_never_fires(monkeypatch):
    calls = _arm(monkeypatch, gate=False)
    assert _drive([_fork()]) == 0                    # gate OFF ⇒ no dispatch
    assert calls == []                               # the STOP-and-ask is untouched — MPR never runs


# ─── gate ON ⇒ fire MPR with the right hints, pre-informed ───────────────────────────────────────────
def test_gate_on_fires_mpr_architecture_panel(monkeypatch):
    calls = _arm(monkeypatch, gate=True)
    assert _drive([_fork(q="how to isolate parallel workers?")]) == 1
    assert len(calls) == 1
    name, args = calls[0]
    assert name == "mpr_research"
    assert args["domain_hint"] == "architecture-decision" and args["mode_hint"] == "decision"
    assert "how to isolate parallel workers?" in args["query"]
    assert "ledger-derived" in args["query"] and "event-seam" in args["query"]   # options carried


def test_query_is_pre_informed_by_prior_bullets(monkeypatch):
    calls = _arm(monkeypatch, gate=True, store=_Store(prior="- [b-1] prefer worktree isolation #arch"))
    _drive([_fork()])
    assert "prefer worktree isolation" in calls[0][1]["query"]   # MPR-A-5: seeded from the playbook


# ─── exactly-once + robustness ───────────────────────────────────────────────────────────────────────
def test_exactly_once_no_redispatch(monkeypatch):
    calls = _arm(monkeypatch, gate=True)
    ledger = [_fork(unit="701")]
    assert _drive(ledger) == 1
    assert _drive(ledger) == 0                        # already dispatched (persisted fork-key set)
    assert len(calls) == 1


def test_tampered_ledger_and_mpr_failure_are_failsoft(monkeypatch):
    # a chain-tampered ledger never dispatches
    calls = _arm(monkeypatch, gate=True)
    assert _drive([_fork()], chain_errors=["hash mismatch"]) == 0 and calls == []
    # and if MPR itself raises, the scan/worker never crash (the ask still surfaces)
    def _boom(name, args): raise RuntimeError("mpr blew up")
    calls2 = _arm(monkeypatch, gate=True, tool=_boom)
    assert _drive([_fork(unit="702")]) == 1           # dispatched; the worker swallows the MPR failure
    assert gx10._ACE_FORK_WORKER.errors == 0          # _ace_fork_run_task caught it (no worker error)


def test_no_options_or_empty_fork_still_safe(monkeypatch):
    calls = _arm(monkeypatch, gate=True)
    assert _drive([{"surface": FORK_SURFACE, "unit": "9", "question": "q only"}]) == 1
    assert "q only" in calls[0][1]["query"]
    # an empty/garbage ledger dispatches nothing, never raises
    assert _drive([None, {}, "x"]) == 0


# ─── #904: the exactly-once key is committed AFTER the run completes, not at dispatch ────────────────
def test_fork_key_committed_after_run_not_at_dispatch(monkeypatch):
    _arm(monkeypatch, gate=True)
    assert gx10._ace_scan_fork_signals([_fork(unit="880")], []) == 1
    assert gx10._ace_load_fork_submitted() == set()       # #904: NOT committed at dispatch (still in-flight)
    gx10._ACE_FORK_WORKER.process_pending()               # run the MPR task to completion
    assert gx10._ace_load_fork_submitted() != set()       # committed only now
    assert gx10._ace_scan_fork_signals([_fork(unit="880")], []) == 0   # re-scan skips the committed fork


def test_queue_drop_is_retried_not_lost(monkeypatch):
    class _Full:
        def submit(self, item): return False              # queue always full → drop
        def process_pending(self): return 0
        def stop(self): pass
    _arm(monkeypatch, gate=True)
    gx10._ACE_FORK_WORKER = _Full()
    assert gx10._ace_scan_fork_signals([_fork(unit="701")], []) == 0   # dropped ⇒ not dispatched
    assert gx10._ace_load_fork_submitted() == set() and not gx10._ACE_FORK_INFLIGHT   # #904: NOT committed
    # a later scan with a working worker retries the same fork — the proposal is never lost
    gx10._ACE_FORK_WORKER = ReflectionWorker(gx10._ace_fork_run_task)
    assert gx10._ace_scan_fork_signals([_fork(unit="701")], []) == 1


def test_fork_worker_torn_down_on_gate_off(tmp_path, monkeypatch):
    on = dict(gx10._code_defaults()); on["ace"] = {"fork_mpr": {"enabled": True}}
    gx10._apply_config(on)
    assert gx10._ACE_FORK_MPR is True and gx10._ACE_FORK_WORKER is not None   # worker exists while gate ON
    off = dict(gx10._code_defaults()); off["ace"] = {"fork_mpr": {"enabled": False}}
    gx10._apply_config(off)
    assert gx10._ACE_FORK_MPR is False and gx10._ACE_FORK_WORKER is None      # #904: torn down on gate OFF
