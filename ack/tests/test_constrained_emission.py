"""Agent-Contract-Kernel — Constrained Emission tests (ACK component 2).

The hard-floor guarantee, proven without a running model: the constrained request
carries the closed schema + a forcing tool_choice, and the runtime validator
REJECTS any reply that omits a required field — on both the native-tool-call path
(vLLM/Qwen3-hermes) and the content-JSON fallback path (our live Ollama runtime).

    python -m pytest core/ack/tests/test_constrained_emission.py -v
"""
from __future__ import annotations

import json

import pytest

from ack.case_spec import EXAMPLE_TASK_JSON, TaskSpec, lint_schema_for_xgrammar
from ack.constrained_emission import (
    ConstrainedEmissionError,
    build_function_tool,
    emit_constrained,
    extract_tool_call,
    json_schema_response_format,
    recommended_vllm_server_flags,
    tool_emission_kwargs,
)
from ack.lodestar.spec import CapabilityTaskSpec

TOOL = "emit_task"

#: A capability-bearing example for the Lodestar CapabilityTaskSpec path.
#: The generic EXAMPLE_TASK_JSON (ack.case_spec) no longer carries a 'capability'
#: key, so capability-under-test cases build their own explicit payload here.
EXAMPLE_CAPABILITY_TASK_JSON: dict = dict(
    EXAMPLE_TASK_JSON, type="implementation", capability="ack-case-spec"
)


# --------------------------------------------------------------------------- #
# helpers — fake chat-completion responses (no network / no model)
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# (1·grammar floor) the request constrains the model to the closed schema
# --------------------------------------------------------------------------- #


def test_function_tool_carries_closed_schema():
    tool = build_function_tool(TaskSpec, name=TOOL, description="emit a task_json")
    assert tool["type"] == "function"
    params = tool["function"]["parameters"]
    # required keys + closed object == the XGrammar hard floor.
    assert params["required"] == ["type", "priority", "title", "description"]
    assert params["additionalProperties"] is False
    # ...and the schema fed to the grammar must be lint-clean.
    assert lint_schema_for_xgrammar(params) == []


def test_tool_emission_forces_named_tool_by_default():
    kwargs = tool_emission_kwargs(TaskSpec, tool_name=TOOL)
    assert kwargs["tool_choice"] == {"type": "function", "function": {"name": TOOL}}
    assert kwargs["tools"][0]["function"]["name"] == TOOL


def test_tool_emission_required_mode_for_allowlist():
    kwargs = tool_emission_kwargs(TaskSpec, tool_name=TOOL, force=False)
    assert kwargs["tool_choice"] == "required"


def test_json_schema_response_format_is_http_shape_not_offline():
    rf = json_schema_response_format(TaskSpec, name="task")["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["schema"]["required"][0] == "type"
    # strict defaults off (spec has optional fields → OpenAI strict would reject).
    assert "strict" not in rf["json_schema"]
    # this is NOT the offline structured_outputs/guided_* shape.
    assert "structured_outputs" not in rf and "guided_json" not in rf


def test_grammar_unsafe_schema_fails_loud_before_egress():
    bad = {"type": "object", "properties": {"tags": {"type": "array", "minItems": 1}}}
    with pytest.raises(ConstrainedEmissionError, match="minItems"):
        build_function_tool(bad, name="x")


# --------------------------------------------------------------------------- #
# (2·runtime floor) required fields are NOT omittable — both transport paths
# --------------------------------------------------------------------------- #


def test_complete_tool_call_validates():
    spec = emit_constrained(TaskSpec, _tool_call_response(EXAMPLE_TASK_JSON), tool_name=TOOL)
    assert isinstance(spec, TaskSpec)
    assert spec.id == "TASK-1"


def test_complete_capability_tool_call_validates():
    """The capability field lives on the Lodestar CapabilityTaskSpec, not the base
    TaskSpec — assert it round-trips there with a capability-bearing example."""
    spec = emit_constrained(
        CapabilityTaskSpec,
        _tool_call_response(EXAMPLE_CAPABILITY_TASK_JSON),
        tool_name=TOOL,
    )
    assert isinstance(spec, CapabilityTaskSpec)
    assert spec.capability == "ack-case-spec"


def test_missing_required_field_is_rejected_tool_path():
    """The whole point: drop a required key → fail-closed with the exact field."""
    incomplete = {k: v for k, v in EXAMPLE_TASK_JSON.items() if k != "title"}
    with pytest.raises(ConstrainedEmissionError) as exc:
        emit_constrained(TaskSpec, _tool_call_response(incomplete), tool_name=TOOL)
    assert "title" in (exc.value.detail or "")


def test_missing_required_field_is_rejected_content_path():
    """Same guarantee on the no-native-tool-calls backend (Ollama)."""
    incomplete = {k: v for k, v in EXAMPLE_TASK_JSON.items() if k != "priority"}
    with pytest.raises(ConstrainedEmissionError) as exc:
        emit_constrained(TaskSpec, _content_response(incomplete))
    assert "priority" in (exc.value.detail or "")


def test_missing_conditional_capability_is_rejected():
    """The KGC-535 failure mode: buildable type without capability → rejected.

    Capability-being-mandatory is a Lodestar contract, so this MUST exercise
    CapabilityTaskSpec — the base TaskSpec has no 'capability' field and would
    happily build this buildable payload.
    """
    payload = {"type": "implementation", "priority": "high", "title": "x", "description": "y"}
    with pytest.raises(ConstrainedEmissionError) as exc:
        emit_constrained(CapabilityTaskSpec, _tool_call_response(payload), tool_name=TOOL)
    assert "capability" in (exc.value.detail or "")


def test_extra_key_is_rejected():
    payload = dict(EXAMPLE_TASK_JSON, bogus="boom")
    with pytest.raises(ConstrainedEmissionError):
        emit_constrained(TaskSpec, _tool_call_response(payload), tool_name=TOOL)


# --------------------------------------------------------------------------- #
# extraction robustness — fail-closed on malformed transport replies
# --------------------------------------------------------------------------- #


def test_content_fallback_strips_code_fence():
    fenced = "```json\n" + json.dumps(EXAMPLE_TASK_JSON) + "\n```"
    spec = emit_constrained(TaskSpec, _content_response(fenced))
    assert spec.id == "TASK-1"


def test_unparseable_arguments_raise():
    with pytest.raises(ConstrainedEmissionError, match="parseable"):
        extract_tool_call(_tool_call_response("{not json"), tool_name=TOOL)


def test_wrong_tool_name_raises():
    with pytest.raises(ConstrainedEmissionError, match="no tool_call"):
        extract_tool_call(_tool_call_response(EXAMPLE_TASK_JSON, name="other"), tool_name=TOOL)


def test_missing_message_raises():
    with pytest.raises(ConstrainedEmissionError, match="choices"):
        extract_tool_call({"choices": []}, tool_name=TOOL)


def test_no_tool_calls_and_no_content_raises():
    resp = {"choices": [{"message": {"role": "assistant"}}]}
    with pytest.raises(ConstrainedEmissionError, match="neither"):
        extract_tool_call(resp, tool_name=TOOL)


# --------------------------------------------------------------------------- #
# ops grounding — vLLM launch flags per model family
# --------------------------------------------------------------------------- #


def test_qwen3_uses_hermes_parser():
    flags = recommended_vllm_server_flags("qwen3")
    assert flags == ["--enable-auto-tool-choice", "--tool-call-parser", "hermes"]


def test_qwen3_coder_uses_xml_parser():
    assert recommended_vllm_server_flags("qwen3-coder")[-1] == "qwen3_xml"


def test_unknown_model_family_raises():
    with pytest.raises(ValueError, match="unknown model family"):
        recommended_vllm_server_flags("gpt-9")
