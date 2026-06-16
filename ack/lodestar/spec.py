"""Lodestar spec extension — the capability contract on top of the generic TaskSpec.

The generic :class:`ack.case_spec.TaskSpec` carries no domain requirement. Lodestar
(opt-in) adds the **capability** dimension: a ``capability`` key that is *mandatory*
for buildable/feature-tracked task types, so capability tracking stays drift-free
(this is exactly the "forgot the capability key" failure the kernel exists to make
impossible). When Lodestar is disabled the engine uses the plain base spec and the
key simply does not exist; when enabled it uses :class:`CapabilityTaskSpec`.
"""
from __future__ import annotations

from typing import Optional

from pydantic import Field, model_validator

from ..case_spec import TaskSpec, TaskType

#: Types for which ``capability`` is **mandatory** — the buildable/feature-tracked
#: kinds that drive capability tracking. For other types (architecture,
#: documentation, security-audit, …) ``capability`` is allowed but optional.
CAPABILITY_REQUIRED_TYPES: frozenset[TaskType] = frozenset(
    {
        TaskType.IMPLEMENTATION,
        TaskType.FEATURE,
        TaskType.BACKEND,
        TaskType.FRONTEND,
        TaskType.FULLSTACK,
        TaskType.INTEGRATION,
    }
)


class CapabilityTaskSpec(TaskSpec):
    """:class:`TaskSpec` + the Lodestar capability contract.

    Adds the ``capability`` field and a cross-field validator making it mandatory
    for :data:`CAPABILITY_REQUIRED_TYPES`. Inherits ``extra='forbid'`` /
    ``use_enum_values=True`` from the base, so the schema stays closed and grammar-
    safe; the extra field is simply now part of the allowed surface.
    """

    capability: Optional[str] = Field(
        default=None,
        description=(
            "Capability/feature key (e.g. 'ack-case-spec'). MANDATORY for buildable "
            "types so capability tracking stays drift-free; optional otherwise."
        ),
    )

    @model_validator(mode="after")
    def _capability_required_for_buildable(self) -> "CapabilityTaskSpec":
        # ``use_enum_values=True`` stores raw strings; compare on ``.value``.
        type_value = self.type if isinstance(self.type, str) else self.type.value
        required = {t.value for t in CAPABILITY_REQUIRED_TYPES}
        if type_value in required and not (self.capability or "").strip():
            raise ValueError(
                f"'capability' is mandatory for type='{type_value}' "
                f"(buildable types: {sorted(required)})"
            )
        return self


def capability_prompt_rule() -> str:
    """The hard-rule line to append to a prompt block (via ``extra_rules``) so the
    model is told ``capability`` is mandatory for buildable types — kept here, not in
    the generic prompt builder, so the kernel stays capability-agnostic."""
    return (
        "'capability' is MANDATORY when 'type' is one of: "
        f"{sorted(t.value for t in CAPABILITY_REQUIRED_TYPES)}."
    )
