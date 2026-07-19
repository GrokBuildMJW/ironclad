"""ACE-DEVPROOF (#855 / #881, M4-4) — the M4 capstone: a boundary-clean END-TO-END proof that multiple
dev-process producers learn from the ledger-derived trajectory. Drives the full M4 stack (ledger →
`ack.ace.devtraj` → the gx10 M4-2 scan → the real `ReflectionWorker` → `PlaybookStore.adapt` → a playbook
mutation) for the same shared ledger schema — one adapter serves every conforming producer (DP-3). Also
proves DP-4 (tampered/absent ledger ⇒ no crash, no learning), exactly-once (no double-learning vs
#863/M4-0), and the used-bullet correlation (M4-3).
Reads the ledger as plain data and imports no producer-specific implementation.
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
from ack import hooks
from ack import lessons as L
from ack.ace import ReflectionWorker, HELPFUL
from playbook_store import PlaybookStore, record_unit_bullets


def _leg(unit, src, dst, guard, passed, reasons=None):
    return {"unit": unit, "src": src, "dst": dst, "guard": guard, "passed": passed, "reasons": reasons or []}


# a terminal (merged) unit — the SAME driver record schema the public ack.devprocess driver AND the internal
# scripts/devprocess/driver.py both emit (boundary-clean: this is the ledger data contract, not a private import)
def _merged_unit_ledger(issue):
    return [_leg(issue, "IMPLEMENT", "GATE", "gate", True),
            _leg(issue, "GATE", "REVIEW", "review-evidence", True),
            _leg(issue, "REVIEW", "MERGE", "merge-go", True)]


def _chat(insight="validate the boundary before the gate", section="strategies_and_hard_rules", ratings=None):
    payload = json.dumps({"insights": [{"content": insight, "section": section}], "ratings": ratings or []})
    return lambda prompt: payload


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
    monkeypatch.setattr(project_registry, "ironclad_home", lambda: tmp_path)
    yield
    _hard_reset()
    gx10._EFFECTIVE_CFG = saved


def _setup(tmp_path, monkeypatch, chat):
    """Wire the REAL end-to-end chain: a real PlaybookStore (with an injected model) + a real ReflectionWorker
    driving the real _ace_run_task, a bound scope, the home for the submitted-set + correlation map."""
    store = gx10._ACE_STORE = PlaybookStore(tmp_path / "pb")
    store.set_transports(chat=chat)
    gx10._ACE_WORKER = ReflectionWorker(gx10._ace_run_task)     # the real worker; drained synchronously below
    monkeypatch.setattr(gx10, "_active_mem_ns", lambda default="": "ns", raising=False)
    return store


def _drive(payloads, chain_errors=None):
    """Run the M4-2 scan then drain the worker synchronously (the full learn path) — returns #units submitted."""
    n = gx10._ace_scan_dev_ledger(payloads, chain_errors if chain_errors is not None else [])
    gx10._ACE_WORKER.process_pending()                         # _ace_run_task → PlaybookStore.adapt → mutation
    return n


# ─── DP-3: BOTH dev-processes learn (same ledger schema, one adapter) ────────────────────────────────
def test_public_generic_devprocess_learns_end_to_end(tmp_path, monkeypatch):
    store = _setup(tmp_path, monkeypatch, _chat("public-path lesson"))
    n = _drive(_merged_unit_ledger(701))                       # a ledger a public ack.devprocess driver wrote
    assert n == 1
    assert "public-path lesson" in store.get_lessons("ns")     # the playbook MUTATED → the dev-process learned


def test_internal_dev3_learns_end_to_end(tmp_path, monkeypatch):
    store = _setup(tmp_path, monkeypatch, _chat("internal-DEV3 lesson"))
    n = _drive(_merged_unit_ledger(702))                       # the SAME schema the internal scripts/devloop emits
    assert n == 1
    assert "internal-DEV3 lesson" in store.get_lessons("ns")   # one adapter (M4-1/2/3) serves both paths (DP-3)


# ─── DP-4: a degraded / tampered / absent ledger never crashes + never learns ────────────────────────
def test_tampered_ledger_no_crash_no_learning(tmp_path, monkeypatch):
    store = _setup(tmp_path, monkeypatch, _chat())
    n = _drive(_merged_unit_ledger(703), chain_errors=["record 2: hash mismatch (payload tampered)"])
    assert n == 0 and store.get_lessons("ns") == []            # fail-closed: a corrupt ledger is no learning source


def test_absent_and_garbage_ledger_no_learning(tmp_path, monkeypatch):
    store = _setup(tmp_path, monkeypatch, _chat())
    assert _drive([]) == 0 and _drive([None, "x", {}, {"unit": 9}]) == 0
    assert store.get_lessons("ns") == []


# ─── exactly-once: no double-learning across re-scans / vs #863's per-handover path ──────────────────
def test_exactly_once_no_double_learning(tmp_path, monkeypatch):
    store = _setup(tmp_path, monkeypatch, _chat("once"))
    ledger = _merged_unit_ledger(704)
    assert _drive(ledger) == 1
    before = len(store.get_lessons("ns", limit=100))
    assert _drive(ledger) == 0                                 # re-scan: the unit was already submitted (persisted)
    assert len(store.get_lessons("ns", limit=100)) == before   # no second adaptation for the same unit
    # the per-UNIT ledger trajectory is a DIFFERENT signal than the per-handover post_feedback hook (#863/M4-0,
    # keyed by task_id) — the scan keys on the unit's MERGE arc, so a unit's work is not counted twice.


# ─── M4-3 correlation flows end-to-end: a used bullet is RATED ───────────────────────────────────────
def test_used_bullet_is_rated_end_to_end(tmp_path, monkeypatch):
    store = _setup(tmp_path, monkeypatch, None)
    seed = store._load("ns"); b = seed.add_bullet("a prior strategy", "strategies_and_hard_rules"); store._save("ns", seed)
    record_unit_bullets(tmp_path, "705", [b.id])               # unit #705's handover injected this bullet (M4-3)
    # the reflector rates the used bullet HELPFUL on a successful unit
    store.set_transports(chat=_chat(insight="new lesson", ratings=[{"bullet_id": b.id, "verdict": HELPFUL}]))
    _drive(_merged_unit_ledger(705))
    assert store._load("ns").get(b.id).helpful_count >= 1      # E-004: the unit's used bullet was rated helpful
