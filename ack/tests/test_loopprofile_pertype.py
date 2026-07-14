"""#602 2.6 / #807 — per-TaskType loop profile on the dev-task pipeline (the first live by_type consumer).

`gx10._failover_budget(task_id)` resolves the code-agent failover attempt budget per the active task's TYPE:
`loop_profiles.by_type[<type>].retry_budget` layered over `strategy.budget` (default) and clamped to the hard
re-ask ceiling — so a per-type override can only LOWER it (escalate sooner). Empty `by_type` → the default
(byte-identical to the flat #806 budget). The finite strategy is always on.
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
from ack.strategy import StrategyAction


class _FakeStore:
    def __init__(self, ttype):
        self._t = ttype

    def get(self, tid):
        return {"id": tid, "type": self._t}


@pytest.fixture(autouse=True)
def _reset():
    gx10._FAILURE_ATTEMPTS.clear()
    gx10._LAST_STRATEGY = None
    yield
    gx10._FAILURE_ATTEMPTS.clear()
    gx10._LAST_STRATEGY = None
    gx10._apply_config(gx10._code_defaults())


def _apply(monkeypatch, *, task_type, by_type=None):
    cfg = gx10._code_defaults()
    if by_type:
        cfg["loop_profiles"]["by_type"] = by_type
    gx10._apply_config(cfg)                                   # sets bounded _STRATEGY_BUDGET
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", cfg)          # so _failover_budget sees loop_profiles
    monkeypatch.setattr(gx10, "_store", lambda: _FakeStore(task_type))


def test_per_type_budget_lowers_escalation(monkeypatch):
    # a 'bug' task with by_type retry_budget=1 escalates on the FIRST failure (budget lowered from the default)
    _apply(monkeypatch, task_type="bug", by_type={"bug": {"retry_budget": 1}})
    act = gx10._revise_on_failure("KGC-1", providers.RESULT_FAILED)   # attempt 1, budget 1 → escalate
    assert act == StrategyAction.HUMAN_ESCALATION.value
    assert gx10._last_strategy().escalate is True


def test_unoverridden_type_uses_the_default_budget(monkeypatch):
    # a 'feature' task (no by_type override) keeps the default budget (3) → attempt 1 is NOT an escalation
    _apply(monkeypatch, task_type="feature", by_type={"bug": {"retry_budget": 1}})
    act = gx10._revise_on_failure("KGC-2", providers.RESULT_FAILED)   # attempt 1 of default 3
    assert act != StrategyAction.HUMAN_ESCALATION.value


def test_no_by_type_is_byte_identical_default(monkeypatch):
    # no loop_profiles.by_type → default budget (3) for any type, exactly like the flat #806 budget
    _apply(monkeypatch, task_type="bug")
    gx10._revise_on_failure("KGC-3", providers.RESULT_FAILED)         # 1
    gx10._revise_on_failure("KGC-3", providers.RESULT_FAILED)         # 2
    act = gx10._revise_on_failure("KGC-3", providers.RESULT_FAILED)   # 3 == default budget → escalate
    assert act == StrategyAction.HUMAN_ESCALATION.value
