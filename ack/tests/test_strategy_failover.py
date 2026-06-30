"""#602 2.5 / #806 — Strategy Revisor consumed at the code-agent failover.

`gx10._revise_on_failure(task_id, result_cls)` consults the pure Strategy policy on a code-agent run result
(per-task attempt vs `strategy.budget`) and surfaces a HUMAN_ESCALATION when the budget is spent — instead of
an endless silent failover. Uses the FRESH result class (no stale `_last_failure_class`); a success resets the
task's counter. OPT-IN per `strategy.enabled` (default OFF → None, byte-identical).
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


@pytest.fixture(autouse=True)
def _reset():
    gx10._FAILURE_ATTEMPTS.clear()
    gx10._LAST_STRATEGY = None
    yield
    gx10._FAILURE_ATTEMPTS.clear()
    gx10._LAST_STRATEGY = None
    gx10._apply_config(gx10._code_defaults())   # restore defaults → strategy off


def _enable(on, budget=3):
    cfg = gx10._code_defaults()
    if on:
        cfg["strategy"]["enabled"] = True
        cfg["strategy"]["budget"] = budget
    gx10._apply_config(cfg)


def test_budget_spent_escalates_to_human():
    _enable(True, budget=3)
    gx10._revise_on_failure("KGC-1", providers.RESULT_UNAVAILABLE)          # attempt 1
    gx10._revise_on_failure("KGC-1", providers.RESULT_UNAVAILABLE)          # attempt 2
    act = gx10._revise_on_failure("KGC-1", providers.RESULT_UNAVAILABLE)    # attempt 3 == budget → escalate
    assert act == StrategyAction.HUMAN_ESCALATION.value
    assert gx10._last_strategy() is not None and gx10._last_strategy().escalate is True


def test_first_attempt_is_a_targeted_action_not_escalation():
    _enable(True, budget=3)
    act = gx10._revise_on_failure("KGC-2", providers.RESULT_FAILED)         # attempt 1 of 3
    assert act is not None and act != StrategyAction.HUMAN_ESCALATION.value
    assert gx10._last_strategy().escalate is False


def test_success_resets_the_attempt_counter():
    _enable(True, budget=2)
    gx10._revise_on_failure("KGC-3", providers.RESULT_FAILED)              # attempt 1
    assert gx10._revise_on_failure("KGC-3", providers.RESULT_OK) is None    # OK → reset, no strategy
    # after the reset the next failure is attempt 1 again — NOT attempt 2 → no premature escalation
    act = gx10._revise_on_failure("KGC-3", providers.RESULT_FAILED)
    assert act != StrategyAction.HUMAN_ESCALATION.value


def test_default_off_is_byte_identical():
    _enable(False)
    assert gx10._revise_on_failure("KGC-4", providers.RESULT_UNAVAILABLE) is None
    assert gx10._last_strategy() is None
    assert gx10._FAILURE_ATTEMPTS == {}                                    # opt-in: nothing counted when off
