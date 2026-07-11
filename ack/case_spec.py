"""Case-Spec (SSOT) — Agent-Contract-Kernel component 1 (generic).

> **One source of truth.** A single Pydantic-v2 model (:class:`TaskSpec`) describes
> a task. ``model_json_schema()`` then feeds *all four* downstream consumers from
> that one source:
>
>   1. **Prompt-Block**     — :func:`prompt_block_from_schema`
>   2. **vLLM-Constraint**   — :func:`vllm_structured_output_config`
>   3. **Runtime-Validator** — :class:`TaskSpec` / :func:`validate_task_json`
>   4. **Docs / Scaffold**   — the schema + this module's docstrings

The fix the kernel embodies is **enforcement, not model-training**:

  * **Syntax floor** — a closed JSON-Schema (``additionalProperties: false`` +
    ``required``) fed to a grammar-constrained decoder makes required fields
    *token-physically* impossible to omit (XGrammar logit-masking, model-agnostic).
  * **Semantics** — cross-field rules a grammar cannot carry (e.g. ISO timestamps)
    live in Pydantic validators here.

This module is the **generic** base: ``type / priority / title / description`` and
common optional fields, with no vessel- or domain-specific requirement baked in.
Opinionated extensions (e.g. the Lodestar capability/gap plugin, which makes a
``capability`` key mandatory for buildable types) subclass :class:`TaskSpec` and
add their own field + validator — they are opt-in, never forced on the base.

**XGrammar constraint (verified).** XGrammar V1 *400s* on the array keywords
``minItems / maxItems / uniqueItems / contains`` (+ ``min/maxContains``). So
cardinality rules live in **validators**, never in the schema;
:func:`lint_schema_for_xgrammar` asserts the generated schema stays grammar-clean.

**vLLM API (verified).** The legacy ``guided_json`` / ``guided_*`` sampling fields
were removed in vLLM **v0.12.0**; the current API is
``structured_outputs={"json": <schema>}`` — see :func:`vllm_structured_output_config`.

Pure (pydantic + stdlib only — no DB, no I/O), so it imports standalone and is safe
to use from prompts, tests, the doctor CLI and the executor.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Iterable, Optional, Type

from pydantic import BaseModel, ConfigDict, Field, field_validator

# --------------------------------------------------------------------------- #
# Shared shapes / vocabularies (the "$defs registry" feeding the JSON-Schema)
# --------------------------------------------------------------------------- #

#: Task-ID format. Generic: an uppercase prefix + a number (e.g. ``KGC-549``,
#: ``TASK-12``). The orchestrator's TaskStore mints IDs (and owns the concrete
#: prefix via ``tasks.id_prefix``); the spec only validates the *shape* when an
#: id/dependency is present — it never mints one.
TASK_ID_PATTERN = r"^[A-Z][A-Z0-9]*-\d+$"

#: Known agent identities (informational only — ``assigned_to`` stays an open
#: string so new agents do not require a schema change; Registry-Discovery owns
#: the live set). Vessels extend this freely.
KNOWN_ASSIGNEES: tuple[str, ...] = (
    "claude-opus-4-8",
    "claude-sonnet-5",
)


class TaskType(str, Enum):
    """Task category — a generic software-work vocabulary (override per vessel)."""

    IMPLEMENTATION = "implementation"
    FEATURE = "feature"
    BACKEND = "backend"
    FRONTEND = "frontend"
    FULLSTACK = "fullstack"
    INTEGRATION = "integration"
    REFACTORING = "refactoring"
    BUGFIX = "bugfix"
    OPTIMIZATION = "optimization"
    SECURITY = "security"
    SECURITY_AUDIT = "security-audit"
    ARCHITECTURE = "architecture"
    VERIFICATION = "verification"
    DEPLOYMENT = "deployment"
    INFRASTRUCTURE = "infrastructure"
    DOCUMENTATION = "documentation"
    CONCEPT = "concept"
    RESEARCH = "research"
    CLEANUP = "cleanup"
    SMOKE_TEST = "smoke-test"
    # #1296: the tracker-level parent record of a design decomposition (1:1 GitHub epic). Never
    # buildable/launchable itself — children link to it via ``parent``; the engine closes it
    # deterministically when the last child is done.
    EPIC = "epic"


class Priority(str, Enum):
    """Task priority."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    NORMAL = "normal"
    LOW = "low"


class TaskStatus(str, Enum):
    """Lifecycle status. The *directory* under ``tasks/`` is the real truth
    (orchestrator-owned); this mirrors the canonical 3-state set for validation."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"


# --------------------------------------------------------------------------- #
# The Case-Spec — generic SSOT for task_json
# --------------------------------------------------------------------------- #


class TaskSpec(BaseModel):
    """Single-Source-of-Truth contract for a task creation payload (``task_json``).

    Scope = the **agent-emitted creation contract**. Lifecycle annotations the
    orchestrator adds *later* (``completion_note``, ``sprint`` …) are intentionally
    **out of scope** and rejected here (``extra='forbid'``) so the grammar stays
    closed and typos surface at emission, not three steps downstream.

    Required: ``type``, ``priority``, ``title``, ``description``. Everything else is
    optional. Opinionated requirements (e.g. a mandatory ``capability`` for buildable
    types) belong in a subclass — see :mod:`ack.lodestar.spec`.
    """

    # ``extra='forbid'`` → ``additionalProperties: false`` in the schema → XGrammar
    # can fully close the object so the model cannot emit junk keys.
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    # --- required ---------------------------------------------------------- #
    type: TaskType = Field(description="Task category — drives routing, effort and gates.")
    priority: Priority = Field(description="Scheduling priority.")
    title: str = Field(description="One-line task title (non-empty).")
    description: str = Field(description="What is to be done and why (non-empty).")

    # --- optional ---------------------------------------------------------- #
    dependencies: list[str] = Field(
        default_factory=list,
        description="Task-IDs this task depends on (each a valid Task-ID).",
    )
    # S1 (#1223): the GitHub-issue-shaped fields — a unit carries the label set + the epic/parent link so it
    # maps 1:1 onto a (sub-)issue. Additive + optional (empty defaults → byte-identical when unused).
    labels: list[str] = Field(
        default_factory=list,
        description="Free-form labels on the unit (the 1:1 GitHub-issue label set).",
    )
    parent: Optional[str] = Field(
        default=None,
        description="The parent/epic this unit belongs to (the 1:1 GitHub epic/sub-issue link); a unit-id or issue ref.",
    )
    acceptance_criteria: Optional[list[str]] = Field(
        default=None,
        description="Concrete, checkable acceptance criteria (no empty entries).",
    )
    zero_trust_impact: Optional[str] = Field(
        default=None,
        description="Security/Zero-Trust impact statement, or an explicit 'none'.",
    )
    assigned_to: Optional[str] = Field(
        default=None,
        description="Agent identity this task is routed to (open string).",
    )
    # NOTE: kept a plain string (not ``datetime``) on purpose — keeps the emitted
    # schema a bare ``string`` (grammar-friendly); ISO-8601 is enforced in a
    # validator, not via a ``format`` keyword the model would have to satisfy.
    created_at: Optional[str] = Field(
        default=None, description="Creation timestamp, ISO-8601 (UTC, e.g. 2026-06-16T10:09:05Z)."
    )
    status: TaskStatus = Field(
        default=TaskStatus.PENDING,
        description="Lifecycle status (directory under tasks/ is the real truth).",
    )
    id: Optional[str] = Field(
        default=None,
        description="Task-ID. Minted by the TaskStore, not the agent.",
    )
    # #1341 (epic #1344 S5): optional machine-checkable constraint fields. Declared
    # explicitly because ``extra='forbid'`` rejects undeclared keys. Absent → validates
    # identically to pre-typed task_json (byte-identical). No conditional-required logic
    # here (that is S6 hard-check).
    language: Optional[str] = Field(
        default=None,
        description="Optional implementation-language constraint (allow-listed token).",
    )
    network: Optional[bool] = Field(
        default=None,
        description="Optional network-access constraint (true=allowed, false=forbidden).",
    )

    # --- field validators -------------------------------------------------- #
    @field_validator("title", "description")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if v is None or not v.strip():
            raise ValueError("must be a non-empty string")
        return v.strip()

    @field_validator("dependencies")
    @classmethod
    def _valid_dep_ids(cls, deps: list[str]) -> list[str]:
        for d in deps:
            if not re.match(TASK_ID_PATTERN, d or ""):
                raise ValueError(f"dependency {d!r} is not a valid Task-ID")
        return deps

    @field_validator("parent")
    @classmethod
    def _valid_parent(cls, p: "Optional[str]") -> "Optional[str]":
        # S1 (#1223): the parent/epic link must be a real unit-id (like `dependencies`) so it maps cleanly onto
        # a (sub-)issue — an empty/None parent is "no epic"; anything else must be a valid Task-ID.
        if p is None or not p.strip():
            return None
        p = p.strip()
        if not re.match(TASK_ID_PATTERN, p):
            raise ValueError(f"parent {p!r} is not a valid unit/Task-ID (e.g. KGC-3)")
        return p

    @field_validator("acceptance_criteria")
    @classmethod
    def _criteria_non_empty(cls, crit: Optional[list[str]]) -> Optional[list[str]]:
        if crit is None:
            return None
        for c in crit:
            if not (c or "").strip():
                raise ValueError("acceptance_criteria must not contain empty entries")
        return crit

    @field_validator("id")
    @classmethod
    def _valid_id(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not re.match(TASK_ID_PATTERN, v):
            raise ValueError(f"id {v!r} is not a valid Task-ID")
        return v

    @field_validator("created_at")
    @classmethod
    def _valid_iso(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        try:
            datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"created_at {v!r} is not valid ISO-8601") from exc
        return v


# --------------------------------------------------------------------------- #
# (4·Docs) Schema derivation
# --------------------------------------------------------------------------- #


def task_spec_json_schema(spec_cls: Type[BaseModel] = TaskSpec) -> dict[str, Any]:
    """Return the JSON-Schema for *spec_cls* (shared shapes land in ``$defs``)."""
    return spec_cls.model_json_schema()


# --------------------------------------------------------------------------- #
# (1·Constraint) XGrammar lint — keep the schema grammar-safe
# --------------------------------------------------------------------------- #

#: Keywords XGrammar V1 does **not** support and that *400* the vLLM request.
#: Such rules must be enforced in Pydantic validators, never in the grammar.
UNSUPPORTED_XGRAMMAR_KEYWORDS: frozenset[str] = frozenset(
    {"minItems", "maxItems", "uniqueItems", "contains", "minContains", "maxContains"}
)


@dataclass(frozen=True)
class LintFinding:
    """One unsupported keyword located in a JSON-Schema."""

    path: str
    keyword: str
    message: str

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return f"{self.path}: unsupported '{self.keyword}' — {self.message}"


def lint_schema_for_xgrammar(schema: dict[str, Any]) -> list[LintFinding]:
    """Walk *schema* (incl. ``$defs``) and report XGrammar-unsupported keywords.

    Empty result == grammar-safe. Used as a regression guard: the schema fed to
    vLLM must never carry array-cardinality keywords (they 400 under XGrammar V1);
    enforce those rules in validators instead.
    """
    findings: list[LintFinding] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in UNSUPPORTED_XGRAMMAR_KEYWORDS:
                    findings.append(
                        LintFinding(
                            path=f"{path}/{key}" if path else key,
                            keyword=key,
                            message="move this rule into a Pydantic validator; XGrammar V1 rejects it (HTTP 400)",
                        )
                    )
                walk(value, f"{path}/{key}" if path else str(key))
        elif isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, f"{path}/{i}")

    walk(schema, "")
    return findings


# --------------------------------------------------------------------------- #
# (2·Prompt) Prompt-Block derivation
# --------------------------------------------------------------------------- #


def _resolve_ref(schema: dict[str, Any], ref: str) -> dict[str, Any]:
    """Resolve a local ``#/$defs/Name`` reference within *schema*."""
    if not ref.startswith("#/"):
        return {}
    node: Any = schema
    for part in ref[2:].split("/"):
        if not isinstance(node, dict):
            return {}
        node = node.get(part, {})
    return node if isinstance(node, dict) else {}


def _describe_type(schema: dict[str, Any], prop: dict[str, Any]) -> str:
    """Human/LLM-readable one-liner of a property's allowed type/values."""
    if "$ref" in prop:
        target = _resolve_ref(schema, prop["$ref"])
        if "enum" in target:
            return "one of: " + ", ".join(map(str, target["enum"]))
        return target.get("type", "object")
    if "anyOf" in prop:
        parts = [p for p in prop["anyOf"] if p.get("type") != "null"]
        if len(parts) == 1:
            return _describe_type(schema, parts[0])
        return " | ".join(_describe_type(schema, p) for p in parts)
    if "enum" in prop:
        return "one of: " + ", ".join(map(str, prop["enum"]))
    t = prop.get("type", "any")
    if t == "array":
        return f"array of {_describe_type(schema, prop.get('items', {}))}"
    return t


def prompt_block_from_schema(
    schema: dict[str, Any], *, extra_rules: Optional[Iterable[str]] = None
) -> str:
    """Derive an LLM prompt block (required fields + constraints) from *schema*.

    This is the **same** schema fed to the grammar — prompt and constraint can never
    drift because both come from ``model_json_schema()``. Callers / plugins may pass
    ``extra_rules`` to append domain-specific hard rules (e.g. Lodestar's mandatory
    ``capability`` rule) without this generic module knowing about them.
    """
    required = set(schema.get("required", []))
    props: dict[str, Any] = schema.get("properties", {})
    lines: list[str] = [
        "You MUST emit a single JSON object matching this contract.",
        "",
        "Required fields (must always be present):",
    ]
    for name in schema.get("required", []):
        prop = props.get(name, {})
        desc = prop.get("description", "")
        lines.append(f"  - {name} ({_describe_type(schema, prop)}): {desc}".rstrip())

    optional = [n for n in props if n not in required]
    if optional:
        lines.append("")
        lines.append("Optional fields:")
        for name in optional:
            prop = props[name]
            desc = prop.get("description", "")
            lines.append(f"  - {name} ({_describe_type(schema, prop)}): {desc}".rstrip())

    lines += [
        "",
        "Hard rules (enforced — output is rejected otherwise):",
        "  - 'dependencies' entries must be valid Task-IDs.",
        "  - No extra keys beyond those listed above.",
        "  - 'title' and 'description' must be non-empty.",
    ]
    for rule in extra_rules or ():
        lines.append(f"  - {rule}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# (3·Constraint) vLLM structured-output config
# --------------------------------------------------------------------------- #


def vllm_structured_output_config(
    schema: Optional[dict[str, Any]] = None,
    *,
    tool_choice: Optional[str] = None,
) -> dict[str, Any]:
    """Build the vLLM v0.12.0+ structured-output kwargs for grammar-constrained decode.

    Uses ``structured_outputs={"json": schema}`` (legacy ``guided_*`` removed in
    v0.12.0). Pass ``tool_choice`` (e.g. ``"required"`` or a named tool) when
    constraining tool-calls instead of a bare JSON object. The returned schema is
    lint-clean by construction (see :func:`lint_schema_for_xgrammar`).

    NOTE: grammar-constrained decode currently crashes on the GB10/Blackwell target
    (XGrammar Triton bitmask in CUDA-graph capture) — the engine runs the SOFT path
    (:mod:`ack.validated_emit`) until that is resolved. This config is the parked
    hard-floor, kept ready.
    """
    schema = schema if schema is not None else task_spec_json_schema()
    cfg: dict[str, Any] = {"structured_outputs": {"json": schema}}
    if tool_choice is not None:
        cfg["tool_choice"] = tool_choice
    return cfg


# --------------------------------------------------------------------------- #
# Convenience validator + canonical example
# --------------------------------------------------------------------------- #


def validate_task_json(data: dict[str, Any], spec_cls: Type[BaseModel] = TaskSpec) -> BaseModel:
    """Validate a raw ``task_json`` dict against *spec_cls* (default :class:`TaskSpec`).

    Raises :class:`pydantic.ValidationError` with the *exact* offending field(s) —
    that precise error is what the Validated-Emit loop (ACK component 3) feeds back
    to the model on a re-ask.
    """
    return spec_cls.model_validate(data)


#: Canonical example ``task_json`` (generic — validates against the base TaskSpec).
EXAMPLE_TASK_JSON: dict[str, Any] = {
    "type": "architecture",
    "priority": "high",
    "title": "Define the agent-contract-kernel case spec",
    "description": (
        "A Pydantic-v2 model as the single source of truth for task_json. "
        "model_json_schema() feeds prompt + vLLM constraint + validator + docs."
    ),
    "dependencies": [],
    "acceptance_criteria": [
        "Pydantic-v2 model created",
        "JSON-Schema generation + XGrammar lint implemented",
    ],
    "zero_trust_impact": "none — prevents malformed task configs.",
    "assigned_to": "claude-opus-4-8",
    "created_at": "2026-06-16T10:09:05Z",
    "status": "in_progress",
    "id": "TASK-1",
}


def _print_artifacts() -> None:  # pragma: no cover
    """Demo entry point: derive all four artefacts from the one schema."""
    import json

    schema = task_spec_json_schema()
    print("=== JSON-Schema ===")
    print(json.dumps(schema, indent=2, ensure_ascii=False))
    print("\n=== XGrammar lint ===")
    findings = lint_schema_for_xgrammar(schema)
    print("clean (no unsupported keywords)" if not findings else "\n".join(map(str, findings)))
    print("\n=== Prompt-Block ===")
    print(prompt_block_from_schema(schema))
    print("\n=== vLLM config (keys) ===")
    print(list(vllm_structured_output_config(schema).keys()))
    print("\n=== Example validates ===")
    print(validate_task_json(EXAMPLE_TASK_JSON).model_dump(exclude_none=True))


if __name__ == "__main__":  # pragma: no cover
    _print_artifacts()
