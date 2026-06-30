"""Failure Classification — shared taxonomy tests (ACK, #602 S602-3).

Proves, with no model and no network:

  * the :class:`FailureClass` enum is a stable, string-valued SSOT (snapshot);
  * :func:`classify_emission_failure` deterministically maps the ACTUAL
    emission/validation error strings (built by calling the real
    :mod:`ack.constrained_emission` / :mod:`ack.validated_emit` code, not hand-typed)
    onto the right class — including the prefix-collision guard that keeps a *parse*
    failure of ``tool_call.arguments`` out of the call-envelope bucket;
  * the engine ``providers.result_failure_class`` bridge re-maps the 3-class run
    taxonomy onto the same enum (one SSOT), total over the known results;
  * the validated-emit loop attaches the class on a terminal failure and leaves it
    ``None`` on success (byte-identical to the pre-#602 shape).

    python -m pytest ack/tests/test_failure_class.py -q
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from ack import validated_emit as vemit
from ack.case_spec import EXAMPLE_TASK_JSON, TaskSpec
from ack.constrained_emission import (
    ConstrainedEmissionError,
    emit_constrained,
    extract_tool_call,
)
from ack.lodestar.spec import CapabilityTaskSpec
from ack.failure_class import FailureClass, classify_emission_failure
from ack.validated_emit import (
    ValidatedEmitResult,
    emit_validated,
    require_min_acceptance_criteria,
)

# The bridge under test lives in the engine; put core/engine on the path like the
# other engine-touching ACK tests (test_providers.py).
_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import providers  # noqa: E402

TOOL = "emit_task"


# --------------------------------------------------------------------------- #
# helpers — build the REAL errors / responses, never hand-typed strings
# --------------------------------------------------------------------------- #
def _captured(fn) -> ConstrainedEmissionError:
    """Run *fn* and return the ConstrainedEmissionError it raises (or fail)."""
    try:
        fn()
    except ConstrainedEmissionError as exc:
        return exc
    raise AssertionError("expected a ConstrainedEmissionError")


def _classify(exc: ConstrainedEmissionError) -> FailureClass:
    """Classify exactly the way the re-ask loop does: message + detail."""
    return classify_emission_failure(str(exc), exc.detail)


def _tool_call_response(args, *, name: str = TOOL) -> dict:
    arguments = args if isinstance(args, str) else json.dumps(args)
    return {"choices": [{"message": {"role": "assistant", "tool_calls": [
        {"type": "function", "function": {"name": name, "arguments": arguments}}]}}]}


def _content_response(content) -> dict:
    body = content if isinstance(content, str) else json.dumps(content)
    return {"choices": [{"message": {"role": "assistant", "content": body}}]}


class _FakeChat:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def __call__(self, *, messages, model=None, temperature=None, extra_body=None):
        self.calls.append({"messages": messages})
        return self._responses.pop(0)


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# the enum is the SSOT — stable, string-valued
# --------------------------------------------------------------------------- #
def test_failure_class_member_values_are_stable():
    """Snapshot: the 8 members and their wire values are the contract for lesson
    metadata / logs — a rename or reorder must be a conscious break."""
    assert {m.name: m.value for m in FailureClass} == {
        "MISSING_CONTEXT": "missing_context",
        "BAD_TOOL_ARGS": "bad_tool_args",
        "RETRIEVAL_FAILURE": "retrieval_failure",
        "HALLUCINATED_ASSUMPTION": "hallucinated_assumption",
        "INCOMPLETE_OUTPUT": "incomplete_output",
        "POLICY_CONFLICT": "policy_conflict",
        "SCHEMA_INVALID": "schema_invalid",
        "UNAVAILABLE": "unavailable",
    }


def test_failure_class_is_str_enum_and_json_serialises_plain():
    assert FailureClass.UNAVAILABLE == "unavailable"
    assert json.dumps({"fc": FailureClass.BAD_TOOL_ARGS}) == '{"fc": "bad_tool_args"}'


# --------------------------------------------------------------------------- #
# classify_emission_failure — over the REAL error strings
# --------------------------------------------------------------------------- #
def test_no_matching_tool_call_is_bad_tool_args():
    exc = _captured(lambda: extract_tool_call(
        _tool_call_response(EXAMPLE_TASK_JSON, name="some_other_tool"), tool_name=TOOL))
    assert _classify(exc) is FailureClass.BAD_TOOL_ARGS


def test_neither_tool_calls_nor_content_is_bad_tool_args():
    exc = _captured(lambda: extract_tool_call(
        {"choices": [{"message": {"role": "assistant"}}]}, tool_name=TOOL))
    assert _classify(exc) is FailureClass.BAD_TOOL_ARGS


def test_missing_message_envelope_is_bad_tool_args():
    exc = _captured(lambda: extract_tool_call({"choices": []}, tool_name=TOOL))
    assert _classify(exc) is FailureClass.BAD_TOOL_ARGS


def test_unparseable_tool_args_is_schema_invalid_not_envelope():
    """Prefix-collision guard: the error text starts with 'tool_call.arguments:' but it
    is a PARSE failure, not a call-envelope failure → SCHEMA_INVALID, not BAD_TOOL_ARGS."""
    exc = _captured(lambda: extract_tool_call(
        _tool_call_response("{not valid json"), tool_name=TOOL))
    assert "tool_call" in str(exc).lower()  # the colliding prefix really is present
    assert _classify(exc) is FailureClass.SCHEMA_INVALID


def test_non_object_content_is_schema_invalid():
    exc = _captured(lambda: extract_tool_call(_content_response([1, 2, 3]), tool_name=TOOL))
    assert _classify(exc) is FailureClass.SCHEMA_INVALID


def test_wrong_type_payload_is_schema_invalid():
    """A wrong-shape value (a non-string title) is the WRONG shape → SCHEMA_INVALID."""
    bad_type = dict(EXAMPLE_TASK_JSON, title=123)
    exc = _captured(lambda: emit_constrained(
        TaskSpec, _tool_call_response(bad_type), tool_name=TOOL))
    assert _classify(exc) is FailureClass.SCHEMA_INVALID


def test_invalid_enum_value_is_schema_invalid():
    bad_enum = dict(EXAMPLE_TASK_JSON, priority="nonsense")
    exc = _captured(lambda: emit_constrained(
        TaskSpec, _tool_call_response(bad_enum), tool_name=TOOL))
    assert _classify(exc) is FailureClass.SCHEMA_INVALID


def test_missing_required_field_is_incomplete_output():
    """A missing REQUIRED field is an under-filled (right-shape) object → INCOMPLETE,
    NOT SCHEMA_INVALID — consistent with the conditional/empty-required cases below."""
    incomplete = {k: v for k, v in EXAMPLE_TASK_JSON.items() if k != "priority"}
    exc = _captured(lambda: emit_constrained(
        TaskSpec, _tool_call_response(incomplete), tool_name=TOOL))
    assert _classify(exc) is FailureClass.INCOMPLETE_OUTPUT


def test_empty_required_string_is_incomplete_output():
    """An empty required string ("must be a non-empty string") is under-filled."""
    empty_title = dict(EXAMPLE_TASK_JSON, title="")
    exc = _captured(lambda: emit_constrained(
        TaskSpec, _tool_call_response(empty_title), tool_name=TOOL))
    assert _classify(exc) is FailureClass.INCOMPLETE_OUTPUT


def test_conditional_required_field_is_incomplete_output():
    """The lodestar cross-field rule ("'capability' is mandatory for type=...") is a
    missing-required-given-context → under-filled → INCOMPLETE."""
    no_cap = {"type": "feature", "priority": "high", "title": "x", "description": "y"}
    exc = _captured(lambda: emit_constrained(
        CapabilityTaskSpec, _tool_call_response(no_cap), tool_name=TOOL))
    assert _classify(exc) is FailureClass.INCOMPLETE_OUTPUT


def test_cardinality_validator_detail_is_incomplete_output():
    """A real require_min_acceptance_criteria rejection ('...must have at least N
    entries') is INCOMPLETE_OUTPUT, not SCHEMA_INVALID — even though the loop's wrapper
    message also mentions a validator."""
    spec = TaskSpec(**dict(EXAMPLE_TASK_JSON, acceptance_criteria=["only one"]))
    validator = require_min_acceptance_criteria(2)
    try:
        validator(spec)
        raise AssertionError("validator should have rejected one criterion")
    except ValueError as exc:
        detail = str(exc)
    assert classify_emission_failure(
        "semantic validator rejected the emission", detail
    ) is FailureClass.INCOMPLETE_OUTPUT


def test_pydantic_too_few_items_is_incomplete_output():
    """A Pydantic cardinality error string ('...should have at least N items') also
    carries 'validation' — the INCOMPLETE rule is checked first so it wins."""
    assert classify_emission_failure(
        "X validation error", "List should have at least 2 items after validation"
    ) is FailureClass.INCOMPLETE_OUTPUT


def test_unknown_failure_defaults_to_schema_invalid():
    assert classify_emission_failure("something nobody anticipated") is FailureClass.SCHEMA_INVALID


def test_classify_is_pure_on_empty_and_none():
    # never raises; the conservative default holds for empty / None inputs.
    assert classify_emission_failure("", None) is FailureClass.SCHEMA_INVALID
    assert classify_emission_failure("anything", None) is FailureClass.SCHEMA_INVALID


def test_classify_never_raises_on_hostile_input():
    """The 'Never raises' contract holds even for a directly-passed hostile message/detail (str()/format
    raises) — the outer backstop returns the conservative SCHEMA_INVALID (symmetric with strategy.revise)."""
    class _Bad:
        def __str__(self):
            raise RuntimeError("nope")
    assert classify_emission_failure(_Bad(), None) is FailureClass.SCHEMA_INVALID
    assert classify_emission_failure("ok", _Bad()) is FailureClass.SCHEMA_INVALID


# --------------------------------------------------------------------------- #
# providers bridge — one SSOT for the 3-class run taxonomy
# --------------------------------------------------------------------------- #
def test_bridge_maps_unavailable():
    assert providers.result_failure_class(providers.RESULT_UNAVAILABLE) is FailureClass.UNAVAILABLE


def test_bridge_maps_failed_to_incomplete_output():
    assert providers.result_failure_class(providers.RESULT_FAILED) is FailureClass.INCOMPLETE_OUTPUT


def test_bridge_ok_is_not_a_failure():
    assert providers.result_failure_class(providers.RESULT_OK) is None


def test_bridge_is_total_over_known_results_and_safe_on_unknown():
    # Every result string classify_agent_result can return is mapped (OK→None,
    # the two failures→a FailureClass); an unknown string is a safe None.
    known = {providers.RESULT_OK, providers.RESULT_FAILED, providers.RESULT_UNAVAILABLE}
    for r in known:
        mapped = providers.result_failure_class(r)
        assert mapped is None or isinstance(mapped, FailureClass)
    assert providers.result_failure_class("not-a-real-result") is None


# --------------------------------------------------------------------------- #
# validated-emit wiring — set on terminal failure, None on success
# --------------------------------------------------------------------------- #
def test_success_has_no_failure_class():
    fake = _FakeChat([_tool_call_response(EXAMPLE_TASK_JSON)])
    result = _run(emit_validated(
        TaskSpec, chat=fake, messages=[{"role": "user", "content": "go"}], tool_name=TOOL))
    assert result.ok is True
    assert result.failure_class is None  # byte-identical to the pre-#602 shape


def test_terminal_failure_sets_schema_invalid():
    bad_enum = dict(EXAMPLE_TASK_JSON, priority="nonsense")  # wrong-shape → SCHEMA_INVALID
    fake = _FakeChat([_tool_call_response(bad_enum) for _ in range(3)])
    result = _run(emit_validated(
        TaskSpec, chat=fake, messages=[{"role": "user", "content": "go"}], tool_name=TOOL))
    assert result.ok is False
    assert result.failure_class is FailureClass.SCHEMA_INVALID


def test_terminal_failure_sets_bad_tool_args():
    fake = _FakeChat([_tool_call_response(EXAMPLE_TASK_JSON, name="wrong_tool") for _ in range(3)])
    result = _run(emit_validated(
        TaskSpec, chat=fake, messages=[{"role": "user", "content": "go"}], tool_name=TOOL))
    assert result.ok is False
    assert result.failure_class is FailureClass.BAD_TOOL_ARGS


def test_terminal_failure_sets_incomplete_output_via_validator():
    one_crit = dict(EXAMPLE_TASK_JSON, acceptance_criteria=["only one"])
    fake = _FakeChat([_tool_call_response(one_crit) for _ in range(3)])
    result = _run(emit_validated(
        TaskSpec, chat=fake, messages=[{"role": "user", "content": "go"}], tool_name=TOOL,
        validators=[require_min_acceptance_criteria(2)]))
    assert result.ok is False
    assert result.failure_class is FailureClass.INCOMPLETE_OUTPUT


def test_result_default_failure_class_is_none():
    # an explicit construction without the new field defaults to None (frozen dataclass,
    # additive optional → no caller breakage).
    r = ValidatedEmitResult(ok=True, value=None, attempts=1)
    assert r.failure_class is None


def test_failure_class_is_excluded_from_eq_and_hash():
    """The advisory label is ``compare=False`` → result identity (==, hash) is
    byte-identical to the pre-#602 shape; the label is still observable + in repr."""
    base = dict(ok=False, value=None, attempts=3, error="e", detail="d", reasks=("d",))
    a = ValidatedEmitResult(**base, failure_class=FailureClass.SCHEMA_INVALID)
    b = ValidatedEmitResult(**base, failure_class=FailureClass.BAD_TOOL_ARGS)
    c = ValidatedEmitResult(**base)  # failure_class defaults to None
    assert a == b == c
    assert hash(a) == hash(b) == hash(c)
    assert a.failure_class is FailureClass.SCHEMA_INVALID
    assert "failure_class" in repr(a)
