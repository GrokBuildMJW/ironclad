"""ACE-LIVEFEEDBACK (#855 / #877, M4-0) — the live always-on hook now threads the REAL injected bullet ids
+ a real label-free outcome and learns from FAILING advances (not only successes). Pins: the `- [id]` parse,
the bounded record/take map, and the `_ace_consumer_hook` gate (success / genuine-failure submit; already-done
+ trivial-precondition-error skip; missing-map ⇒ empty used_bullet_ids).
"""
from __future__ import annotations

from design_test_support import approve_active_design

import sys
import types
from pathlib import Path

import pytest

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=lambda **kw: object()))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import project_context as pc
from project_context import ProjectContext
import project_registry
import gx10
from ack import hooks
from ack import lessons as L
from playbook_store import PlaybookStore


class _FakeAgent:
    def run(self, t): pass
    def save_session(self): pass
    def status(self): return "ok"


class _RecWorker:
    """A stand-in ReflectionWorker that records what the hook submits (no background thread)."""

    def __init__(self):
        self.items = []

    def submit(self, item):
        self.items.append(item)
        return True


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


# ─── pure helpers ────────────────────────────────────────────────────────────────────────────────────
def test_ace_bullet_ids_parses_leading_id_tokens():
    rendered = "[strategies_and_hard_rules]\n- [b-0] always validate  #known_bad_strategy\n- [b-12] cache it\nnoise [x] not-a-bullet"
    assert gx10._ace_bullet_ids(rendered) == ["b-0", "b-12"]
    assert gx10._ace_bullet_ids("") == [] and gx10._ace_bullet_ids(None) == []


def test_record_and_take_is_pop_semantics_and_bounded():
    gx10._ace_record_injected("T-1", ["b-0", "b-1"])
    assert gx10._ace_take_injected("T-1") == ["b-0", "b-1"]
    assert gx10._ace_take_injected("T-1") == []            # popped — second take is empty
    gx10._ace_record_injected("", ["b-0"]); gx10._ace_record_injected("T-2", [])
    assert gx10._ace_take_injected("T-2") == []            # empty task_id / empty ids are no-ops
    for i in range(gx10._ACE_INJECTED_CAP + 50):           # stays bounded
        gx10._ace_record_injected(f"K-{i}", ["b-0"])
    assert len(gx10._ACE_INJECTED) <= gx10._ACE_INJECTED_CAP


# ─── the consumer gate (direct, deterministic) ───────────────────────────────────────────────────────
def _arm_consumer(monkeypatch, tmp_path):
    """Wire the minimum the hook needs: an ACE store + a recording worker + a bound scope + a stub task store."""
    gx10._ACE_STORE = PlaybookStore(tmp_path / "pb")
    rec = gx10._ACE_WORKER = _RecWorker()
    monkeypatch.setattr(gx10, "_active_mem_ns", lambda default="": "ns", raising=False)
    monkeypatch.setattr(gx10, "_store", lambda: types.SimpleNamespace(get=lambda tid: {"title": "Build X", "type": "feature"}), raising=False)
    monkeypatch.setattr(gx10, "archive_feedback_dir", lambda: tmp_path, raising=False)   # no bound ctx in this unit
    monkeypatch.chdir(tmp_path)
    return rec


def test_success_submits_success_with_real_used_bullets(monkeypatch, tmp_path):
    rec = _arm_consumer(monkeypatch, tmp_path)
    gx10._ace_record_injected("T-1", ["b-0", "b-2"])       # the bullets staged into T-1's handover
    gx10._ace_consumer_hook({"result": "OK: pipeline advanced for T-1 (OPUS)", "task_id": "T-1", "agent": "OPUS"})
    assert len(rec.items) == 1
    traj = rec.items[0]["trajectory"]
    assert traj.outcome == "success" and traj.used_bullet_ids == ["b-0", "b-2"]
    assert gx10._ace_take_injected("T-1") == []            # consumed (popped)


def test_genuine_failure_submits_failed_and_learns(monkeypatch, tmp_path):
    rec = _arm_consumer(monkeypatch, tmp_path)
    gx10._ace_record_injected("T-2", ["b-5"])
    gx10._ace_consumer_hook({"result": "ERROR: pipeline step failed: boom\nso far:\n  - x",
                             "task_id": "T-2", "agent": "OPUS"})
    assert len(rec.items) == 1
    traj = rec.items[0]["trajectory"]
    assert traj.outcome == "failed" and traj.used_bullet_ids == ["b-5"]   # E-001/O-002 + E-004 harmful signal
    assert any("pipeline step failed" in s for s in traj.steps)


def test_already_done_and_trivial_errors_do_not_submit(monkeypatch, tmp_path):
    rec = _arm_consumer(monkeypatch, tmp_path)
    gx10._ace_consumer_hook({"result": "OK: task T-3 is already done — no re-advance needed.", "task_id": "T-3", "agent": "OPUS"})
    gx10._ace_consumer_hook({"result": "ERROR: invalid task_id: 'x'", "task_id": "", "agent": "OPUS"})
    gx10._ace_consumer_hook({"result": "ERROR: feedback missing: ...", "task_id": "T-4", "agent": "OPUS"})
    assert rec.items == []                                  # none of these is a real attempt → no learning


def test_missing_injection_map_yields_empty_used(monkeypatch, tmp_path):
    rec = _arm_consumer(monkeypatch, tmp_path)              # no _ace_record_injected for this task
    gx10._ace_consumer_hook({"result": "OK: pipeline advanced for T-9 (OPUS)", "task_id": "T-9", "agent": "OPUS"})
    assert len(rec.items) == 1 and rec.items[0]["trajectory"].used_bullet_ids == []


def test_no_submit_without_worker_or_scope(monkeypatch, tmp_path):
    # no worker wired → no-op
    gx10._ACE_STORE = PlaybookStore(tmp_path / "pb"); gx10._ACE_WORKER = None
    monkeypatch.setattr(gx10, "_active_mem_ns", lambda default="": "ns", raising=False)
    gx10._ace_consumer_hook({"result": "OK: pipeline advanced for T (OPUS)", "task_id": "T", "agent": "OPUS"})  # no raise
    # worker wired but no bound scope → no-op
    rec = gx10._ACE_WORKER = _RecWorker()
    monkeypatch.setattr(gx10, "_active_mem_ns", lambda default="": "", raising=False)
    gx10._ace_consumer_hook({"result": "OK: pipeline advanced for T (OPUS)", "task_id": "T", "agent": "OPUS"})
    assert rec.items == []


# ─── injection-site capture (integration via the real stage path) ────────────────────────────────────
def test_stage_handover_records_injected_bullets(monkeypatch, tmp_path):
    gx10._apply_config(gx10._code_defaults())
    gx10._ACE_STORE.report_lesson("ns", "always validate the parser input before staging")
    gx10.STORE = None
    monkeypatch.chdir(tmp_path)
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        gx10._dispatch(_FakeAgent(), "initiative new Order Service --type software")
        approve_active_design(gx10)
        gx10._stage_handover(None, "OPUS", "## Handover\nbuild it",
                             task_json='{"type":"feature","priority":"high","title":"Build the feedback feature","description":"Build the complete feedback feature through the validated staging pipeline."}',
                             force=True)
        tid = gx10._store().list("pending")[0]["id"]
    # the seeded bullet was injected into the handover → its id is recorded against the task
    assert gx10._ace_take_injected(tid) != []
