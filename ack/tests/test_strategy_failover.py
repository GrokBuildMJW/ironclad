"""#602 2.5 / #806 — Strategy Revisor consumed at the code-agent failover.

`gx10._revise_on_failure(task_id, result_cls)` consults the pure Strategy policy on a code-agent run result
(per-task attempt vs `strategy.budget`) and surfaces a HUMAN_ESCALATION when the budget is spent — instead of
an endless silent failover. Uses the FRESH result class (no stale `_last_failure_class`); a success resets the
task's counter. The strategy is always on; `strategy.budget` remains bounded operational tuning.
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
    gx10._apply_config(gx10._code_defaults())


def _apply(budget=3):
    cfg = gx10._code_defaults()
    cfg["strategy"]["budget"] = budget
    gx10._apply_config(cfg)


def test_budget_spent_escalates_to_human():
    _apply(budget=3)
    gx10._revise_on_failure("KGC-1", providers.RESULT_UNAVAILABLE)          # attempt 1
    gx10._revise_on_failure("KGC-1", providers.RESULT_UNAVAILABLE)          # attempt 2
    act = gx10._revise_on_failure("KGC-1", providers.RESULT_UNAVAILABLE)    # attempt 3 == budget → escalate
    assert act == StrategyAction.HUMAN_ESCALATION.value
    assert gx10._last_strategy() is not None and gx10._last_strategy().escalate is True


def test_first_attempt_is_a_targeted_action_not_escalation():
    _apply(budget=3)
    act = gx10._revise_on_failure("KGC-2", providers.RESULT_FAILED)         # attempt 1 of 3
    assert act is not None and act != StrategyAction.HUMAN_ESCALATION.value
    assert gx10._last_strategy().escalate is False


def test_success_resets_the_attempt_counter():
    _apply(budget=2)
    gx10._revise_on_failure("KGC-3", providers.RESULT_FAILED)              # attempt 1
    assert gx10._revise_on_failure("KGC-3", providers.RESULT_OK) is None    # OK → reset, no strategy
    # after the reset the next failure is attempt 1 again — NOT attempt 2 → no premature escalation
    act = gx10._revise_on_failure("KGC-3", providers.RESULT_FAILED)
    assert act != StrategyAction.HUMAN_ESCALATION.value


@pytest.mark.parametrize("legacy", [True, False], ids=["legacy-true", "legacy-false"])
def test_strategy_enabled_is_a_warning_only_tombstone_and_cannot_disable(legacy, capsys):
    cfg = gx10._code_defaults()
    assert "enabled" not in cfg["strategy"] and not hasattr(gx10, "_STRATEGY_ENABLED")
    cfg["strategy"]["enabled"] = legacy

    gx10._apply_config(cfg)
    gx10._apply_config(cfg)

    warnings = [line for line in capsys.readouterr().out.splitlines() if "DEPRECATED" in line]
    assert len(warnings) == 1
    assert "strategy.enabled" in warnings[0] and "always on" in warnings[0]
    assert "enabled" not in cfg["strategy"]
    assert gx10._revise_on_failure("KGC-4", providers.RESULT_UNAVAILABLE) == StrategyAction.FAIL_OVER.value
    assert gx10._FAILURE_ATTEMPTS == {"KGC-4": 1}


def test_runtime_set_refuses_retired_strategy_switch(monkeypatch):
    cfg = gx10._code_defaults()
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", cfg)
    surfaced = []
    monkeypatch.setattr(gx10, "_ui_print", lambda message, *a, **k: surfaced.append(str(message)))

    gx10._dispatch(None, "config set strategy.enabled false")

    assert len(surfaced) == 1 and "retired and cannot be set" in surfaced[0]
    assert "enabled" not in cfg["strategy"]
    assert gx10._revise_on_failure("KGC-5", providers.RESULT_FAILED) is not None


@pytest.mark.parametrize("invalid", [0, -1, float("nan"), float("inf"), float("-inf")])
def test_strategy_budget_rejects_non_positive_or_non_finite_values(invalid):
    # Always-on strategy: an invalid budget is schema-REFUSED at apply (fail closed), not silently
    # defaulted — you cannot neuter the strategy via a 0/negative/non-finite budget. Mirrors the
    # heartbeat.stall_seconds sibling. Values over the hard ceiling are refused too.
    with pytest.raises(ValueError, match="strategy.budget"):
        _apply(invalid)
    assert gx10._STRATEGY_BUDGET == 3


def test_strategy_budget_is_capped_by_the_hard_retry_ceiling():
    from ack.validated_emit import MAX_RETRY_BUDGET

    with pytest.raises(ValueError, match="strategy.budget"):
        _apply(MAX_RETRY_BUDGET + 100)
    assert gx10._STRATEGY_BUDGET == MAX_RETRY_BUDGET
