"""#602 2.x / #809 — the closed-loop end-to-end gate (C2-DONE).

Proves the C2 reflection loop is LIVE end-to-end on the dev-task pipeline — every consumer fires, no link is a
no-op (the test the C1 half-ship would have failed): a staged handover is scored (Verifier) → the score feeds
the Quality breaker and trips it → a run failure is classified (FailureClass) → the Strategy Revisor escalates
when the per-task budget is spent. Validation, Quality, and Strategy are always on, and the ACE
loop-intelligence core always owns `post_feedback` (#863). Also proves 8b:
`loop_profiles.by_type[<type>].eval` selects WHICH verifiers run.

(Lessons + Process — the C1 learning half — fire at task completion and are covered by `test_lesson_seam_wiring`
+ `test_process`; this gate focuses on the C2 reflection consumers that #802/#808/#805/#806 wired.)
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

import gx10
import providers
from ack import hooks
from ack.strategy import StrategyAction
from design_test_support import approve_active_design


class _FakeMem:
    def __init__(self, hits):
        self._hits = list(hits)
        self.searched = []

    def is_available(self):
        return True

    def search(self, q, limit=5):
        self.searched.append(q)
        return list(self._hits)


def _agent():
    return types.SimpleNamespace(run=lambda t: None, save_session=lambda: None, status=lambda: "ok")


_GOOD = ('{"type":"feature","priority":"high","title":"Build the order service",'
         '"description":"Implement the full order service with validation, persistence and tests."}')


@pytest.fixture(autouse=True)
def _reset():
    hooks.clear_hooks()
    gx10._set_last_verdict(None)
    gx10._QUALITY_TRIPPED = None
    gx10._QUALITY_BREAKER = None
    gx10._LAST_FAILURE_CLASS = None
    gx10._LAST_STRATEGY = None
    gx10._FAILURE_ATTEMPTS.clear()
    yield
    hooks.clear_hooks()
    gx10._set_last_verdict(None)
    gx10._QUALITY_TRIPPED = None
    gx10._QUALITY_BREAKER = None
    gx10._LAST_FAILURE_CLASS = None
    gx10._LAST_STRATEGY = None
    gx10._FAILURE_ATTEMPTS.clear()
    gx10._apply_config(gx10._code_defaults())


def test_c2_closed_reflection_loop_all_consumers_fire(tmp_path, monkeypatch):
    cfg = gx10._code_defaults()
    cfg["quality"]["min_consecutive"] = 1     # one low score trips
    cfg["quality"]["threshold"] = 0.75        # rules pass (1.0) + a PARTIALLY-grounded advisory → combined < 0.75
    cfg["strategy"]["budget"] = 1             # one failure spends the budget → escalate
    gx10.STORE = None
    monkeypatch.chdir(tmp_path)
    # Memory is AVAILABLE but grounds only the ONE "validation" claim below → a genuine partial (sub-threshold)
    # grounding score (F5a: an empty/no-hit store is "unavailable" and excluded; only real partial grounding
    # produces the low score the quality breaker is meant to trip on).
    partial_mem = types.SimpleNamespace(
        is_available=lambda: True,
        search=lambda q, limit=5: (["stored validation evidence"] if "validation" in q.lower() else []))
    monkeypatch.setattr(gx10, "_MEMORY", partial_mem)
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", cfg)
    gx10._apply_config(cfg)
    gx10._dispatch(_agent(), "initiative new Order Service --type software")
    approve_active_design(gx10)

    # Verifier scores the staged handover (pre_handover) → Quality consumes it + trips (post_handover). Three
    # substantive claims, only one grounded → grounding ~0.33, combined ~0.67 < 0.75.
    out = gx10._stage_handover(
        None, "OPUS",
        "## Handover\n"
        "Implement the order service persistence and repository storage layer end to end.\n"
        "Wire the order service http endpoints and request and response serialization completely.\n"
        "Add the full validation checks across every order service endpoint and payload field.",
        task_json=_GOOD, force=True)
    assert out.startswith("OK")
    snap = gx10._quality_tripped()
    assert snap is not None and snap.tripped          # verify → score → quality trip: ALL fired

    tid = gx10._store().list("pending")[0]["id"]
    # a code-agent run failure → FailureClass produced AND Strategy escalates (budget spent).
    assert gx10._record_failure_class(providers.RESULT_FAILED) is not None
    act = gx10._revise_on_failure(tid, providers.RESULT_FAILED)
    assert gx10._last_failure_class() is not None     # failure classified
    assert act == StrategyAction.HUMAN_ESCALATION.value and gx10._last_strategy().escalate  # strategy fired


def test_legacy_strategy_off_cannot_disable_the_closed_loop(tmp_path, monkeypatch, capsys):
    cfg = gx10._code_defaults()
    cfg["strategy"]["enabled"] = False
    gx10.STORE = None
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(gx10, "_MEMORY", _FakeMem(["grounded"]))
    gx10._apply_config(cfg)
    gx10._dispatch(_agent(), "initiative new Order Service --type software")
    approve_active_design(gx10)
    out = gx10._stage_handover(None, "OPUS", "## Handover\nbuild the complete order service",
                                task_json=_GOOD, force=True)
    assert out.startswith("OK")
    # ACE, Quality, failure classification, and Strategy all remain wired despite the retired false value.
    from ack import lessons as L
    from playbook_store import PlaybookStore
    assert isinstance(L.get_provider(), PlaybookStore)
    assert set(hooks.registered_events()) == {"post_feedback", "post_handover"}
    assert gx10._quality_tripped() is None
    tid = gx10._store().list("pending")[0]["id"]
    assert gx10._record_failure_class(providers.RESULT_FAILED) is not None
    assert gx10._revise_on_failure(tid, providers.RESULT_FAILED) is not None
    assert gx10._last_failure_class() is not None and gx10._last_strategy() is not None
    assert "strategy.enabled" in capsys.readouterr().out


def test_eval_verifiers_selects_which_verifiers_run(tmp_path, monkeypatch):
    # 8b: loop_profiles.by_type.feature.eval=['rules'] → only the rules verifier runs; grounding is NOT invoked
    # (memory.search is never called) even though a memory tier is up and would have grounded.
    cfg = gx10._code_defaults()
    cfg["loop_profiles"]["by_type"] = {"feature": {"eval": ["rules"]}}
    gx10.STORE = None
    monkeypatch.chdir(tmp_path)
    mem = _FakeMem(["a prior memory"])         # would ground if grounding ran
    monkeypatch.setattr(gx10, "_MEMORY", mem)
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", cfg)
    gx10._apply_config(cfg)
    gx10._dispatch(_agent(), "initiative new Order Service --type software")
    approve_active_design(gx10)
    gx10._stage_handover(None, "OPUS",
                         "## Handover\nbuild the order service end to end with full validation and tests",
                         task_json=_GOOD, force=True)
    assert mem.searched == []                  # eval=['rules'] → grounding skipped, no cold-store lookup
