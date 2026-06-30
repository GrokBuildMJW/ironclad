"""Strategy Revisor — the pure failure→action policy + its application seams (ACK, #602 S602-7).

Proves, offline:

  * `revise(failure_class, attempt, budget)` is a deterministic, total SSOT — every FailureClass maps to a
    targeted StrategyAction, a spent budget escalates to a human, and it never raises;
  * the `ack.validated_emit` re-ask seam appends the strategy hint to the next attempt when a strategist is
    given, and is byte-identical (no hint) when it is not — and a strategist error is swallowed;
  * the engine-side application `providers.code_agent_strategy` maps a code-agent run result through the same
    SSOT (RESULT_OK → None).

    python -m pytest ack/tests/test_strategy.py -q
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from ack.failure_class import FailureClass
from ack.strategy import Strategy, StrategyAction, revise
from ack.case_spec import EXAMPLE_TASK_JSON, TaskSpec
from ack.validated_emit import emit_validated

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import providers  # noqa: E402

TOOL = "emit_task"


def _tool_call_response(args, *, name: str = TOOL) -> dict:
    arguments = args if isinstance(args, str) else json.dumps(args)
    return {"choices": [{"message": {"role": "assistant", "tool_calls": [
        {"type": "function", "function": {"name": name, "arguments": arguments}}]}}]}


class _FakeChat:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def __call__(self, *, messages, model=None, temperature=None, extra_body=None):
        self.calls.append({"messages": [dict(m) for m in messages]})
        return self._responses.pop(0)


def _run(coro):
    return asyncio.run(coro)


# ─── pure policy fn ─────────────────────────────────────────────────────────────────────────────────
def test_policy_maps_every_failure_class_midbudget():
    """Snapshot: each class → its targeted action (attempt 1 of 3, budget not spent)."""
    expected = {
        FailureClass.MISSING_CONTEXT: StrategyAction.INJECT_CONTEXT,
        FailureClass.BAD_TOOL_ARGS: StrategyAction.NARROW_OR_CLARIFY,
        FailureClass.RETRIEVAL_FAILURE: StrategyAction.SWITCH_RETRIEVAL,
        FailureClass.HALLUCINATED_ASSUMPTION: StrategyAction.GROUND_THEN_ANSWER,
        FailureClass.INCOMPLETE_OUTPUT: StrategyAction.COMPLETE_OUTPUT,
        FailureClass.POLICY_CONFLICT: StrategyAction.RESOLVE_POLICY,
        FailureClass.SCHEMA_INVALID: StrategyAction.REPAIR_SCHEMA,
        FailureClass.UNAVAILABLE: StrategyAction.FAIL_OVER,
    }
    for fc, action in expected.items():
        assert revise(fc, attempt=1, budget=3).action is action


def test_policy_is_total_over_every_failure_class():
    # no FailureClass member is left without a mapping (mid-budget → never RETRY_SAME for a known class).
    for fc in FailureClass:
        assert revise(fc, attempt=1, budget=3).action is not StrategyAction.RETRY_SAME


def test_spent_budget_escalates_to_human():
    s = revise(FailureClass.SCHEMA_INVALID, attempt=3, budget=3)
    assert s.action is StrategyAction.HUMAN_ESCALATION
    assert s.escalate is True
    assert s.reask_hint == ""


def test_non_terminal_classes_carry_a_reask_hint_except_failover():
    # every actionable (non-terminal) class gives the model a hint, EXCEPT UNAVAILABLE (an engine action).
    for fc in FailureClass:
        s = revise(fc, attempt=1, budget=3)
        if s.action is StrategyAction.FAIL_OVER:
            assert s.reask_hint == ""
        else:
            assert s.reask_hint
        assert s.escalate is False


def test_revise_never_raises_on_bad_attempt_budget():
    assert revise(FailureClass.SCHEMA_INVALID, attempt="x", budget=None).action is StrategyAction.REPAIR_SCHEMA


def test_revise_never_raises_on_overflow_or_unhashable():
    # int(float("inf")) → OverflowError must be swallowed (treated as not-spent).
    assert revise(FailureClass.SCHEMA_INVALID, attempt=1, budget=float("inf")).action is StrategyAction.REPAIR_SCHEMA
    # a non-FailureClass / unhashable input must not raise (dict.get would) → RETRY_SAME fallback.
    assert revise([], attempt=1, budget=3).action is StrategyAction.RETRY_SAME
    assert revise("not-a-class", attempt=1, budget=3).action is StrategyAction.RETRY_SAME


def test_revise_never_raises_on_hostile_int():
    """A custom attempt/budget whose __int__ raises must be swallowed (never-raises is absolute)."""
    class _BadInt:
        def __int__(self):
            raise RuntimeError("nope")

    assert revise(FailureClass.SCHEMA_INVALID, _BadInt(), 3).action is StrategyAction.REPAIR_SCHEMA
    assert revise(FailureClass.SCHEMA_INVALID, 1, _BadInt()).action is StrategyAction.REPAIR_SCHEMA


def test_revise_never_raises_on_hostile_failure_class():
    """An object whose __class__ raises (so isinstance itself raises) must still not escape → RETRY_SAME."""
    class _BadFailure:
        @property
        def __class__(self):
            raise RuntimeError("boom-class")

    assert revise(_BadFailure(), 1, 3).action is StrategyAction.RETRY_SAME


def test_strategy_is_frozen():
    s = revise(FailureClass.BAD_TOOL_ARGS, 1, 3)
    with pytest.raises(Exception):
        s.action = StrategyAction.RETRY_SAME   # frozen dataclass


# ─── validated_emit strategist seam ─────────────────────────────────────────────────────────────────
def _missing_priority():
    return {k: v for k, v in EXAMPLE_TASK_JSON.items() if k != "priority"}


def test_reask_is_byte_identical_without_a_strategist():
    """No strategist (the default) → the re-ask turn carries no 'Strategy:' line (pre-#602 behaviour)."""
    fake = _FakeChat([_tool_call_response(_missing_priority()), _tool_call_response(EXAMPLE_TASK_JSON)])
    result = _run(emit_validated(
        TaskSpec, chat=fake, messages=[{"role": "user", "content": "go"}], tool_name=TOOL))
    assert result.ok is True
    reask_turn = fake.calls[1]["messages"][-1]["content"]
    assert "Strategy:" not in reask_turn


def test_strategist_hint_is_appended_to_the_reask():
    fake = _FakeChat([_tool_call_response(_missing_priority()), _tool_call_response(EXAMPLE_TASK_JSON)])
    result = _run(emit_validated(
        TaskSpec, chat=fake, messages=[{"role": "user", "content": "go"}], tool_name=TOOL,
        strategist=revise))
    assert result.ok is True
    reask_turn = fake.calls[1]["messages"][-1]["content"]
    # a missing required field classifies INCOMPLETE_OUTPUT → COMPLETE_OUTPUT hint.
    assert "Strategy:" in reask_turn
    assert revise(FailureClass.INCOMPLETE_OUTPUT, 1, 3).reask_hint in reask_turn


def test_strategist_error_is_swallowed():
    def _boom(fc, attempt, budget):
        raise RuntimeError("strategist down")

    fake = _FakeChat([_tool_call_response(_missing_priority()), _tool_call_response(EXAMPLE_TASK_JSON)])
    result = _run(emit_validated(
        TaskSpec, chat=fake, messages=[{"role": "user", "content": "go"}], tool_name=TOOL,
        strategist=_boom))
    assert result.ok is True   # advisory: a strategist failure never breaks the loop
    assert "Strategy:" not in fake.calls[1]["messages"][-1]["content"]


def test_strategist_returning_odd_hint_is_swallowed():
    """A Strategy whose reask_hint stringifies by raising must not break the loop (the str() coercion is
    inside the advisory guard)."""
    class _Boom:
        def __str__(self):
            raise RuntimeError("nope")

    def _odd(fc, attempt, budget):
        return Strategy(StrategyAction.REPAIR_SCHEMA, _Boom())   # non-str, raising __str__

    fake = _FakeChat([_tool_call_response(_missing_priority()), _tool_call_response(EXAMPLE_TASK_JSON)])
    result = _run(emit_validated(
        TaskSpec, chat=fake, messages=[{"role": "user", "content": "go"}], tool_name=TOOL,
        strategist=_odd))
    assert result.ok is True
    assert "Strategy:" not in fake.calls[1]["messages"][-1]["content"]


# ─── engine-side application via providers ──────────────────────────────────────────────────────────
def test_code_agent_strategy_maps_unavailable_to_failover():
    s = providers.code_agent_strategy(providers.RESULT_UNAVAILABLE, attempt=1, budget=3)
    assert isinstance(s, Strategy)
    assert s.action is StrategyAction.FAIL_OVER


def test_code_agent_strategy_maps_failed_to_complete_output():
    s = providers.code_agent_strategy(providers.RESULT_FAILED, attempt=1, budget=3)
    assert s.action is StrategyAction.COMPLETE_OUTPUT


def test_code_agent_strategy_ok_is_none():
    assert providers.code_agent_strategy(providers.RESULT_OK) is None


def test_code_agent_strategy_defaults_are_non_terminal():
    """With the DEFAULT attempt/budget, a single classification yields the TARGETED action, not an
    immediate human-escalation (the budget default must keep it non-terminal)."""
    assert providers.code_agent_strategy(providers.RESULT_UNAVAILABLE).action is StrategyAction.FAIL_OVER
    assert providers.code_agent_strategy(providers.RESULT_FAILED).action is StrategyAction.COMPLETE_OUTPUT


def test_code_agent_strategy_spent_budget_escalates():
    s = providers.code_agent_strategy(providers.RESULT_FAILED, attempt=3, budget=3)
    assert s.action is StrategyAction.HUMAN_ESCALATION
