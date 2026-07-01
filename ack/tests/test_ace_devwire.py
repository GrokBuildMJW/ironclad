"""ACE-DEVWIRE (#855 / #879, M4-2): the engine dev-process learn-trigger — scan the dev-loop ledger off the
hot path, submit each newly-TERMINAL unit's Trajectory to the ReflectionWorker exactly-once (persisted), and
skip a tampered ledger fail-closed. Driven with injected fakes (a recording worker + fixture payloads).
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
from ack import hooks
from ack import lessons as L
from playbook_store import PlaybookStore


class _RecWorker:
    def __init__(self):
        self.items = []

    def submit(self, item):
        self.items.append(item)
        return True


def _leg(unit, src, dst, guard, passed, reasons=None):
    return {"unit": unit, "src": src, "dst": dst, "guard": guard, "passed": passed, "reasons": reasons or []}


# a ledger with: unit 100 reaches MERGE, unit 101 aborted, unit 102 blocked-at-GATE (not terminal)
_PAYLOADS = [
    _leg(100, "IMPLEMENT", "GATE", "gate", True),
    _leg(102, "IMPLEMENT", "GATE", "coupling", False, ["core/ boundary"]),
    _leg(100, "REVIEW", "MERGE", "merge-go", True),
    {"abort": 101, "reason": "halted by operator"},
]


def _hard_reset():
    if gx10._ACE_WORKER is not None:
        try:
            gx10._ACE_WORKER.stop()
        except Exception:
            pass
    gx10._ACE_WORKER = None
    gx10._ACE_STORE = None
    gx10._ACE_MIGRATED = False
    gx10._ACE_INJECTED.clear()
    hooks.clear_hooks()
    L.set_provider(None)


@pytest.fixture(autouse=True)
def _reset(tmp_path, monkeypatch):
    _hard_reset()
    saved = gx10._EFFECTIVE_CFG
    monkeypatch.setattr(project_registry, "ironclad_home", lambda: tmp_path)   # devscan set persists under here
    yield
    _hard_reset()
    gx10._EFFECTIVE_CFG = saved


def _arm(monkeypatch, tmp_path):
    gx10._ACE_STORE = PlaybookStore(tmp_path / "pb")
    rec = gx10._ACE_WORKER = _RecWorker()
    monkeypatch.setattr(gx10, "_active_mem_ns", lambda default="": "ns", raising=False)
    return rec


def test_scan_submits_only_terminal_units_exactly_once(monkeypatch, tmp_path):
    rec = _arm(monkeypatch, tmp_path)
    n = gx10._ace_scan_dev_ledger(_PAYLOADS, [])
    assert n == 2                                              # the merged + the aborted unit; NOT the blocked one
    by = {it["trajectory"].query: it["trajectory"] for it in rec.items}
    assert set(by) == {"100", "101"}
    assert by["100"].outcome == "reached-human-merge-gate" and by["101"].outcome == "aborted"
    assert by["100"].used_bullet_ids == []                    # M4-3 populates these
    # re-scan the same ledger → nothing new (exactly-once, persisted submitted-set)
    rec.items.clear()
    assert gx10._ace_scan_dev_ledger(_PAYLOADS, []) == 0 and rec.items == []


def test_exactly_once_survives_a_restart(monkeypatch, tmp_path):
    rec = _arm(monkeypatch, tmp_path)
    assert gx10._ace_scan_dev_ledger(_PAYLOADS, []) == 2
    # simulate a process restart: a brand-new worker, same persisted submitted-set on disk
    rec2 = gx10._ACE_WORKER = _RecWorker()
    assert gx10._ace_scan_dev_ledger(_PAYLOADS, []) == 0 and rec2.items == []


def test_unit_key_persisted_before_submit(monkeypatch, tmp_path):
    # C2 #905: the exactly-once key is persisted BEFORE submit (per-item), so a crash during/after submit
    # cannot re-learn the unit (at-most-once, not the previous at-least-once batched-save window).
    class _RaiseWorker:
        def submit(self, item): raise RuntimeError("worker died mid-submit")
    gx10._ACE_STORE = PlaybookStore(tmp_path / "pb")
    gx10._ACE_WORKER = _RaiseWorker()
    monkeypatch.setattr(gx10, "_active_mem_ns", lambda default="": "ns", raising=False)
    ledger = [_leg(100, "IMPLEMENT", "GATE", "gate", True), _leg(100, "REVIEW", "MERGE", "merge-go", True)]
    gx10._ace_scan_dev_ledger(ledger, [])                      # the raising submit is caught fail-soft
    # the key was saved BEFORE the raising submit → a re-scan with a working worker does NOT re-learn it
    rec = gx10._ACE_WORKER = _RecWorker()
    assert gx10._ace_scan_dev_ledger(ledger, []) == 0 and rec.items == []


def test_newly_terminal_unit_on_a_later_scan_is_submitted(monkeypatch, tmp_path):
    rec = _arm(monkeypatch, tmp_path)
    gx10._ace_scan_dev_ledger(_PAYLOADS, [])                  # submits 100 + 101
    rec.items.clear()
    # unit 102 now reaches MERGE (a later ledger state) → it becomes newly terminal
    later = _PAYLOADS + [_leg(102, "GATE", "MERGE", "merge-go", True)]
    assert gx10._ace_scan_dev_ledger(later, []) == 1
    assert rec.items[0]["trajectory"].query == "102" and rec.items[0]["trajectory"].outcome == "reached-human-merge-gate"


def test_tampered_ledger_is_skipped_fail_closed(monkeypatch, tmp_path):
    rec = _arm(monkeypatch, tmp_path)
    assert gx10._ace_scan_dev_ledger(_PAYLOADS, ["record 3: hash mismatch (payload tampered)"]) == 0
    assert rec.items == []                                    # never learn from a corrupt ledger


def test_no_worker_or_no_scope_is_noop(monkeypatch, tmp_path):
    gx10._ACE_STORE = PlaybookStore(tmp_path / "pb"); gx10._ACE_WORKER = None   # no worker
    monkeypatch.setattr(gx10, "_active_mem_ns", lambda default="": "ns", raising=False)
    assert gx10._ace_scan_dev_ledger(_PAYLOADS, []) == 0      # no raise
    rec = _arm(monkeypatch, tmp_path)
    monkeypatch.setattr(gx10, "_active_mem_ns", lambda default="": "", raising=False)   # no bound scope
    assert gx10._ace_scan_dev_ledger(_PAYLOADS, []) == 0 and rec.items == []


def test_missing_ledger_path_is_empty_noop(monkeypatch, tmp_path):
    rec = _arm(monkeypatch, tmp_path)
    assert gx10._ace_scan_dev_ledger(ledger_path=tmp_path / "nope" / "ledger.jsonl") == 0
    assert rec.items == []


def test_garbage_payloads_never_raise(monkeypatch, tmp_path):
    rec = _arm(monkeypatch, tmp_path)
    assert gx10._ace_scan_dev_ledger([None, "x", {}, {"unit": 7}], []) == 0   # nothing terminal, no crash
    assert rec.items == []
