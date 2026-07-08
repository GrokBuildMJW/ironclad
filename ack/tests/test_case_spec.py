"""Agent-Contract-Kernel — Case-Spec tests (ACK component 1).

Validation script + regression guard for the SSOT ``TaskSpec`` and the kernel
utilities derived from its one schema (lint / prompt / vLLM-constraint / validate).

Adapted to the ack/ API:
  * ``core.spec.case_spec`` -> ``ack.case_spec``
  * ``capability`` moved OUT of the base spec onto ``CapabilityTaskSpec``
    (``ack.lodestar.spec``); capability-mandatory / ``.capability`` tests use it.

  python -m pytest ack/tests/test_case_spec.py -v
"""
from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from ack.case_spec import (
    EXAMPLE_TASK_JSON,
    UNSUPPORTED_XGRAMMAR_KEYWORDS,
    TaskSpec,
    TaskStatus,
    TaskType,
    lint_schema_for_xgrammar,
    prompt_block_from_schema,
    task_spec_json_schema,
    validate_task_json,
    vllm_structured_output_config,
)
from ack.lodestar import (
    CAPABILITY_REQUIRED_TYPES,
    CapabilityTaskSpec,
    capability_prompt_rule,
)


# --------------------------------------------------------------------------- #
# Schema generation + $defs registry
# --------------------------------------------------------------------------- #


def test_schema_generates_with_required_and_defs():
    schema = task_spec_json_schema()
    assert schema["required"] == ["type", "priority", "title", "description"]
    # Shared shapes land in the $defs registry (enums).
    assert {"TaskType", "Priority", "TaskStatus"} <= set(schema["$defs"])
    # Closed object → grammar can fully constrain it.
    assert schema["additionalProperties"] is False


# --------------------------------------------------------------------------- #
# XGrammar lint
# --------------------------------------------------------------------------- #


def test_generated_schema_is_xgrammar_clean():
    """The whole point: TaskSpec's schema must carry NO unsupported keyword."""
    assert lint_schema_for_xgrammar(task_spec_json_schema()) == []


def test_lint_detects_unsupported_keywords_anywhere():
    bad = {
        "type": "object",
        "properties": {
            "tags": {"type": "array", "minItems": 1, "maxItems": 9, "uniqueItems": True},
            "nested": {"$defs": {"X": {"contains": {"type": "string"}}}},
        },
    }
    findings = lint_schema_for_xgrammar(bad)
    keywords = {f.keyword for f in findings}
    assert keywords == {"minItems", "maxItems", "uniqueItems", "contains"}
    # Every unsupported keyword is reported with a locating path.
    assert all(f.path for f in findings)


def test_unsupported_set_documents_the_array_keywords():
    assert {"minItems", "maxItems", "uniqueItems", "contains"} <= UNSUPPORTED_XGRAMMAR_KEYWORDS


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #


def test_example_task_json_validates():
    spec = validate_task_json(EXAMPLE_TASK_JSON)
    # EXAMPLE_TASK_JSON is now generic (id='TASK-1', no 'capability' key).
    assert spec.id == "TASK-1"
    assert not hasattr(spec, "capability")
    # use_enum_values → raw strings on the model.
    assert spec.type == "architecture"
    assert spec.status == "in_progress"


def test_minimal_non_buildable_task_needs_no_capability():
    # capability lives on CapabilityTaskSpec now; documentation is non-buildable.
    spec = CapabilityTaskSpec(
        type="documentation", priority="low", title="Doc", description="Write it"
    )
    assert spec.capability is None
    assert spec.status == TaskStatus.PENDING.value
    assert spec.dependencies == []


# --------------------------------------------------------------------------- #
# Cross-field: capability mandatory for buildable types (the capability fix)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("t", sorted(x.value for x in CAPABILITY_REQUIRED_TYPES))
def test_capability_required_for_buildable_types(t):
    with pytest.raises(ValidationError, match="capability"):
        CapabilityTaskSpec(type=t, priority="high", title="x", description="y")
    # With capability it passes.
    ok = CapabilityTaskSpec(
        type=t, priority="high", title="x", description="y", capability="some-key"
    )
    assert ok.capability == "some-key"


def test_blank_capability_is_rejected_for_buildable():
    with pytest.raises(ValidationError, match="capability"):
        CapabilityTaskSpec(
            type="implementation", priority="high", title="x", description="y", capability="  "
        )


# --------------------------------------------------------------------------- #
# Field validators
# --------------------------------------------------------------------------- #


def test_dependencies_must_be_valid_task_ids():
    with pytest.raises(ValidationError, match="Task-ID"):
        TaskSpec(
            type="documentation",
            priority="low",
            title="x",
            description="y",
            dependencies=["KGC-001", "not-an-id"],
        )


def test_id_must_match_pattern():
    # TASK_ID_PATTERN is now generic '^[A-Z][A-Z0-9]*-\\d+$' (ABC-1 would now be
    # VALID); use a lower-case prefix to genuinely violate the pattern.
    with pytest.raises(ValidationError, match="Task-ID"):
        TaskSpec(type="documentation", priority="low", title="x", description="y", id="abc-1")


def test_empty_title_or_description_rejected():
    with pytest.raises(ValidationError):
        TaskSpec(type="documentation", priority="low", title="   ", description="y")
    with pytest.raises(ValidationError):
        TaskSpec(type="documentation", priority="low", title="x", description="")


def test_created_at_must_be_iso8601():
    with pytest.raises(ValidationError, match="ISO-8601"):
        TaskSpec(
            type="documentation",
            priority="low",
            title="x",
            description="y",
            created_at="16.06.2026",
        )
    # Z-suffixed UTC is accepted.
    ok = TaskSpec(
        type="documentation",
        priority="low",
        title="x",
        description="y",
        created_at="2026-06-16T10:09:05Z",
    )
    assert ok.created_at.endswith("Z")


def test_acceptance_criteria_rejects_empty_entries():
    with pytest.raises(ValidationError):
        TaskSpec(
            type="documentation",
            priority="low",
            title="x",
            description="y",
            acceptance_criteria=["ok", "  "],
        )


def test_extra_fields_forbidden():
    payload = copy.deepcopy(EXAMPLE_TASK_JSON)
    payload["unknown_key"] = "boom"
    with pytest.raises(ValidationError):
        validate_task_json(payload)


# --------------------------------------------------------------------------- #
# Derived artefacts (prompt + vLLM constraint) come from the one schema
# --------------------------------------------------------------------------- #


def test_prompt_block_lists_required_fields_and_capability_rule():
    # The generic base prompt block is capability-agnostic; the capability rule is
    # an opt-in extra_rule supplied by the Lodestar plugin.
    block = prompt_block_from_schema(
        task_spec_json_schema(), extra_rules=[capability_prompt_rule()]
    )
    for field in ("type", "priority", "title", "description"):
        assert field in block
    assert "capability" in block and "MANDATORY" in block
    # Enum values are surfaced to the model.
    assert "architecture" in block


def test_vllm_config_uses_structured_outputs_not_guided():
    cfg = vllm_structured_output_config()
    assert "json" in cfg["structured_outputs"]
    assert cfg["structured_outputs"]["json"]["required"][0] == "type"
    # Legacy API must NOT appear.
    assert "guided_json" not in cfg and "guided_decoding" not in cfg
    # tool_choice is opt-in.
    assert "tool_choice" not in cfg
    assert vllm_structured_output_config(tool_choice="required")["tool_choice"] == "required"


def test_vllm_schema_is_lint_clean():
    cfg = vllm_structured_output_config()
    assert lint_schema_for_xgrammar(cfg["structured_outputs"]["json"]) == []


def test_epic_type_is_valid_and_never_capability_gated():
    # #1296: the epic tracker record — a valid TaskType, grammar-clean, and NOT a buildable type
    # (Lodestar must not demand a capability for it).
    from ack.case_spec import TaskSpec, TaskType
    from ack.lodestar.spec import CAPABILITY_REQUIRED_TYPES, CapabilityTaskSpec

    t = TaskSpec.model_validate({"type": "epic", "priority": "high",
                                 "title": "the design epic", "description": "d"})
    assert t.type == "epic"
    assert TaskType.EPIC not in CAPABILITY_REQUIRED_TYPES
    c = CapabilityTaskSpec.model_validate({"type": "epic", "priority": "high",
                                           "title": "the design epic", "description": "d"})
    assert c.capability is None
    assert lint_schema_for_xgrammar(TaskSpec.model_json_schema()) == []
