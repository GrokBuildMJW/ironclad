"""Agent-Contract-Kernel — Validated-Emit tests (ACK component 3).

The closed loop, proven without a running model: a constrained emission that fails
the Pydantic floor OR a semantic validator is re-asked with the EXACT error and
retried up to a bounded budget; when the budget is spent the loop returns a typed
terminal failure (never a partial object). Also pins the transport seam: the wrapper
reaches the model ONLY through the INJECTED ``chat`` transport with the constraint
kwargs in ``extra_body`` (any auth/vessel stamp lives in that transport, untouched
here).

  python -m pytest core/ack/tests/test_validated_emit.py -q
"""
from __future__ import annotations

import asyncio
import json

import pytest

from ack import validated_emit as vemit
from ack.case_spec import EXAMPLE_TASK_JSON, TaskSpec
from ack.lodestar.spec import CapabilityTaskSpec, capability_prompt_rule
from ack.validated_emit import (
    ValidatedEmitError,
    ValidatedEmitResult,
    emit_task_spec,
    emit_validated,
    require_min_acceptance_criteria,
)

TOOL = "emit_task"

#: A capability-bearing example for the CapabilityTaskSpec path: a buildable type
#: (implementation) so the conditional-capability validator is actually exercised.
EXAMPLE_CAPABILITY_JSON: dict = dict(
    EXAMPLE_TASK_JSON,
    type="implementation",
    capability="ack-validated-emit",
)


def _tool_call_response(args: dict | str, *, name: str = TOOL) -> dict:
    """A vLLM/OpenAI-shaped response carrying a native tool_call."""
    arguments = args if isinstance(args, str) else json.dumps(args)
    return {
        "choices": [
            {"message": {"role": "assistant", "tool_calls": [
                {"type": "function", "function": {"name": name, "arguments": arguments}}
            ]}}
        ]
    }


def _content_response(args: dict | str) -> dict:
    """An Ollama-shaped response that returns the object as message.content."""
    content = args if isinstance(args, str) else json.dumps(args)
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


class _FakeChat:
    """A stand-in for the INJECTED ``chat`` transport. Yields queued responses in
    order and records each call's conversation (so re-ask turns are observable).

    Matches the new transport contract::

        async def chat(*, messages, model, temperature, extra_body) -> dict
    """

    def __init__(self, responses: list[dict]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def __call__(self, *, messages, model=None, temperature=None, extra_body=None):
        # Snapshot the conversation + the constraint kwargs that were splatted.
        self.calls.append({
            "messages": [dict(m) for m in messages],
            "extra_body": extra_body,
            "temperature": temperature,
        })
        if not self._responses:
            raise AssertionError("chat transport called more times than responses queued")
        return self._responses.pop(0)


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# happy path
# --------------------------------------------------------------------------- #


def test_happy_path_first_attempt():
    fake = _FakeChat([_tool_call_response(EXAMPLE_TASK_JSON)])

    result = _run(emit_validated(
        TaskSpec, chat=fake,
        messages=[{"role": "user", "content": "go"}], tool_name=TOOL,
    ))
    assert isinstance(result, ValidatedEmitResult)
    assert result.ok is True
    assert result.attempts == 1
    assert result.reasks == ()
    assert isinstance(result.value, TaskSpec)
    # exactly one model call; the constraint travelled via extra_body, not messages.
    assert len(fake.calls) == 1
    assert fake.calls[0]["extra_body"]["tool_choice"]["function"]["name"] == TOOL
    # deterministic emission by default.
    assert fake.calls[0]["temperature"] == 0.0


def test_happy_path_capability_spec_carries_capability():
    """The capability field lives on CapabilityTaskSpec (not the base TaskSpec); a
    buildable example with a capability validates and the value is preserved."""
    fake = _FakeChat([_tool_call_response(EXAMPLE_CAPABILITY_JSON)])

    result = _run(emit_validated(
        CapabilityTaskSpec, chat=fake,
        messages=[{"role": "user", "content": "go"}], tool_name=TOOL,
    ))
    assert result.ok is True
    assert isinstance(result.value, CapabilityTaskSpec)
    assert result.value.capability == "ack-validated-emit"


def test_works_on_ollama_content_path():
    """No native tool_calls (our live runtime) → content-JSON fallback still validates."""
    fake = _FakeChat([_content_response(EXAMPLE_TASK_JSON)])
    result = _run(emit_validated(
        TaskSpec, chat=fake,
        messages=[{"role": "user", "content": "go"}], tool_name=TOOL,
    ))
    assert result.ok is True and result.value.id == "TASK-1"


# --------------------------------------------------------------------------- #
# reask — syntax (missing required field) then succeed
# --------------------------------------------------------------------------- #


def test_reask_on_missing_required_then_succeeds():
    incomplete = {k: v for k, v in EXAMPLE_TASK_JSON.items() if k != "title"}
    fake = _FakeChat([
        _tool_call_response(incomplete),         # attempt 1 — drops 'title'
        _tool_call_response(EXAMPLE_TASK_JSON),  # attempt 2 — corrected
    ])

    result = _run(emit_validated(
        TaskSpec, chat=fake,
        messages=[{"role": "user", "content": "go"}], tool_name=TOOL,
    ))
    assert result.ok is True
    assert result.attempts == 2
    assert len(result.reasks) == 1
    assert "title" in result.reasks[0]
    # the second call carried a re-ask turn appended to the conversation, and that
    # turn quoted the EXACT validator error (the whole point of validated-emit).
    assert len(fake.calls) == 2
    second_convo = fake.calls[1]["messages"]
    assert len(second_convo) == 2  # original user turn + the re-ask turn
    assert "title" in second_convo[-1]["content"]
    assert "REJECTED" in second_convo[-1]["content"]


# --------------------------------------------------------------------------- #
# reask — semantics: cross-field (conditional capability) + array cardinality
# --------------------------------------------------------------------------- #


def test_reask_on_crossfield_capability_then_succeeds():
    """Buildable type without capability → re-asked, then fixed. The conditional-
    capability rule lives on CapabilityTaskSpec, so the test uses that spec."""
    bad = {"type": "implementation", "priority": "high", "title": "x", "description": "y"}
    good = dict(bad, capability="ack-validated-emit")
    fake = _FakeChat([_tool_call_response(bad), _tool_call_response(good)])

    result = _run(emit_validated(
        CapabilityTaskSpec, chat=fake,
        messages=[{"role": "user", "content": "go"}], tool_name=TOOL,
    ))
    assert result.ok is True
    assert result.value.capability == "ack-validated-emit"
    assert "capability" in result.reasks[0]


def test_reask_on_array_cardinality_validator_then_succeeds():
    """Array cardinality (minItems) cannot live in the XGrammar schema — enforced as
    a semantic validator, and a violation re-asks identically to a Pydantic error."""
    one_crit = dict(EXAMPLE_TASK_JSON, acceptance_criteria=["only one"])
    two_crit = dict(EXAMPLE_TASK_JSON, acceptance_criteria=["a", "b"])
    fake = _FakeChat([_tool_call_response(one_crit), _tool_call_response(two_crit)])

    result = _run(emit_validated(
        TaskSpec, chat=fake,
        messages=[{"role": "user", "content": "go"}], tool_name=TOOL,
        validators=[require_min_acceptance_criteria(2)],
    ))
    assert result.ok is True
    assert len(result.value.acceptance_criteria) == 2
    assert "at least 2" in result.reasks[0]


# --------------------------------------------------------------------------- #
# bounded retry → typed terminal failure
# --------------------------------------------------------------------------- #


def test_bounded_retry_exhausts_to_typed_failure():
    incomplete = {k: v for k, v in EXAMPLE_TASK_JSON.items() if k != "priority"}
    fake = _FakeChat([_tool_call_response(incomplete) for _ in range(3)])

    result = _run(emit_validated(
        TaskSpec, chat=fake,
        messages=[{"role": "user", "content": "go"}], tool_name=TOOL,
    ))
    assert result.ok is False
    assert result.value is None
    assert result.attempts == 3            # the default budget
    assert len(fake.calls) == 3            # exactly budget model calls, no more
    assert "priority" in (result.detail or "")
    assert len(result.reasks) == 3
    # raise_for_status surfaces the typed terminal failure for raise-preferring callers.
    with pytest.raises(ValidatedEmitError) as exc:
        result.raise_for_status()
    assert exc.value.attempts == 3


def test_budget_is_hard_capped():
    """A caller asking for more than MAX_RETRY_BUDGET is clamped, never honoured."""
    fake = _FakeChat([_tool_call_response("{not json") for _ in range(10)])
    result = _run(emit_validated(
        TaskSpec, chat=fake,
        messages=[{"role": "user", "content": "go"}], tool_name=TOOL, budget=99,
    ))
    assert result.ok is False
    assert result.attempts == vemit.MAX_RETRY_BUDGET
    assert len(fake.calls) == vemit.MAX_RETRY_BUDGET


def test_budget_one_is_single_shot():
    incomplete = {k: v for k, v in EXAMPLE_TASK_JSON.items() if k != "title"}
    fake = _FakeChat([_tool_call_response(incomplete)])
    result = _run(emit_validated(
        TaskSpec, chat=fake,
        messages=[{"role": "user", "content": "go"}], tool_name=TOOL, budget=1,
    ))
    assert result.ok is False
    assert result.attempts == 1
    assert len(fake.calls) == 1  # no re-ask turn on a single-shot budget


# --------------------------------------------------------------------------- #
# transport / auth failures are NOT re-asked — they propagate
# --------------------------------------------------------------------------- #


def test_transport_not_configured_propagates():
    class _NotConfigured(Exception):
        pass

    async def _boom(*a, **k):
        raise _NotConfigured("off")

    with pytest.raises(_NotConfigured):
        _run(emit_validated(
            TaskSpec, chat=_boom,
            messages=[{"role": "user", "content": "go"}], tool_name=TOOL,
        ))


def test_transport_error_propagates():
    class _TransportError(Exception):
        pass

    async def _boom(*a, **k):
        raise _TransportError("upstream 502")

    with pytest.raises(_TransportError):
        _run(emit_validated(
            TaskSpec, chat=_boom,
            messages=[{"role": "user", "content": "go"}], tool_name=TOOL,
        ))


# --------------------------------------------------------------------------- #
# emit_task_spec — task_json always flows through the spec
# --------------------------------------------------------------------------- #


def test_emit_task_spec_wires_taskspec_and_seeds_schema():
    fake = _FakeChat([_tool_call_response(EXAMPLE_TASK_JSON, name=vemit.EMIT_TASK_TOOL)])
    result = _run(emit_task_spec(chat=fake, instruction="Create the validated-emit task"))
    assert result.ok is True
    assert isinstance(result.value, TaskSpec)
    # the seed conversation carries a system role-lock + the SSOT-derived schema block.
    seed = fake.calls[0]["messages"]
    assert seed[0]["role"] == "system"
    assert "Create the validated-emit task" in seed[1]["content"]


def test_emit_task_spec_reasks_until_capability_present():
    """Through the CapabilityTaskSpec path the 'forgot capability' failure is
    self-healing: a buildable task missing capability is re-asked, then corrected."""
    bad = {"type": "feature", "priority": "high", "title": "x", "description": "y"}
    good = dict(bad, capability="some-cap")
    fake = _FakeChat([
        _tool_call_response(bad, name=vemit.EMIT_TASK_TOOL),
        _tool_call_response(good, name=vemit.EMIT_TASK_TOOL),
    ])
    result = _run(emit_task_spec(
        chat=fake, instruction="make a feature",
        spec_cls=CapabilityTaskSpec, extra_prompt_rules=[capability_prompt_rule()],
    ))
    assert result.ok is True
    assert result.value.capability == "some-cap"
    assert "capability" in result.reasks[0]
    # the schema block / extra rule advertises the capability hard rule.
    assert "capability" in fake.calls[0]["messages"][1]["content"]
