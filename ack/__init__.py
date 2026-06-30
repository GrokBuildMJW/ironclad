"""Ironclad — Agent-Contract-Kernel (ACK).

Generic, model-agnostic reliability layer for LLM agents: schema-as-SSOT,
validate→reask→retry, registry, doctor, generator. Standalone and secret-free —
imports nothing from a private deployment.

Kernel-level exports (imported eagerly here):
  - case_spec        : generic Pydantic-SSOT task contract + schema machinery
  - constrained_emission : hard-floor constrained tool emission
  - validated_emit   : bounded re-ask loop (soft path) with injectable transport

The **plugin/extension contract** (registry, playbook, prompt, gate, catalogue, i18n) is the
curated, versioned **Extension SDK** — import it from :mod:`ack.sdk` (see ADR-0004 +
``docs/plugin-api.md``). The opt-in ``lodestar`` plugin lives under :mod:`ack.lodestar`.
"""
from __future__ import annotations

from .case_spec import (  # noqa: F401
    KNOWN_ASSIGNEES,
    TASK_ID_PATTERN,
    LintFinding,
    Priority,
    TaskSpec,
    TaskStatus,
    TaskType,
    lint_schema_for_xgrammar,
    prompt_block_from_schema,
    task_spec_json_schema,
    validate_task_json,
    vllm_structured_output_config,
)
from .constrained_emission import (  # noqa: F401
    THINKING_OFF,
    ConstrainedEmissionError,
    build_function_tool,
    emit_constrained,
    extract_tool_call,
    json_schema_response_format,
    recommended_vllm_server_flags,
    tool_emission_kwargs,
)
from .failure_class import (  # noqa: F401
    FailureClass,
    classify_emission_failure,
)
from .strategy import (  # noqa: F401
    Strategy,
    StrategyAction,
    revise,
)
from .loop_profile import (  # noqa: F401
    LoopProfile,
    resolve_loop_profile,
)
from .verify import (  # noqa: F401
    VerdictResult,
    verify_grounding,
    verify_rules,
    verify_with_judge,
)
from .quality import (  # noqa: F401
    QualityBreaker,
    QualitySnapshot,
)
from .process import (  # noqa: F401
    ProcessLesson,
    ProcessLessonKind,
    ProcessSignal,
    distill_process_lesson,
    format_process_hint,
)
from .validated_emit import (  # noqa: F401
    DEFAULT_RETRY_BUDGET,
    EMIT_TASK_TOOL,
    MAX_RETRY_BUDGET,
    ChatTransport,
    Validator,
    ValidatedEmitError,
    ValidatedEmitResult,
    emit_task_spec,
    emit_validated,
    require_min_acceptance_criteria,
)

__all__ = [
    # case_spec (SSOT + schema machinery)
    "TaskSpec",
    "TaskType",
    "Priority",
    "TaskStatus",
    "TASK_ID_PATTERN",
    "KNOWN_ASSIGNEES",
    "LintFinding",
    "lint_schema_for_xgrammar",
    "prompt_block_from_schema",
    "vllm_structured_output_config",
    "task_spec_json_schema",
    "validate_task_json",
    # constrained_emission (hard floor — usable on cu130-nightly)
    "ConstrainedEmissionError",
    "THINKING_OFF",
    "build_function_tool",
    "tool_emission_kwargs",
    "json_schema_response_format",
    "extract_tool_call",
    "emit_constrained",
    "recommended_vllm_server_flags",
    # failure_class (shared advisory taxonomy — #602)
    "FailureClass",
    "classify_emission_failure",
    # strategy (failure→action policy — #602)
    "Strategy",
    "StrategyAction",
    "revise",
    # loop_profile (per-TaskType loop budgets — #602)
    "LoopProfile",
    "resolve_loop_profile",
    # verify (mark-only evaluation layer — #602)
    "VerdictResult",
    "verify_rules",
    "verify_grounding",
    "verify_with_judge",
    # quality (output-quality circuit breaker — #602)
    "QualityBreaker",
    "QualitySnapshot",
    # process (process-level self-correction policy — #602)
    "ProcessSignal",
    "ProcessLesson",
    "ProcessLessonKind",
    "distill_process_lesson",
    "format_process_hint",
    "emit_validated",
    "emit_task_spec",
    "ValidatedEmitResult",
    "ValidatedEmitError",
    "Validator",
    "ChatTransport",
    "require_min_acceptance_criteria",
    "DEFAULT_RETRY_BUDGET",
    "MAX_RETRY_BUDGET",
    "EMIT_TASK_TOOL",
]
