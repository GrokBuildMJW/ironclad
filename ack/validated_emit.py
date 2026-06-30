"""Validated-Emit (validate → reask → retry) — Agent-Contract-Kernel component 3.

> **The closed loop.** Component 1 (:mod:`ack.case_spec`) owns the SSOT schema;
> component 2 (:mod:`ack.constrained_emission`) turns it into a grammar-constrained
> request and re-validates the reply *fail-closed*. This module wraps that hard floor
> with the **bounded re-ask loop** that makes the emission *self-correcting*: on a
> syntax OR semantic rejection it feeds the model the EXACT validator error and
> retries, up to a small budget; when the budget is spent it returns a **typed
> terminal failure** — a deterministic completion signal, never a silent or
> partially-valid object.

Why this exists: the hard floor guarantees a single reply is structurally complete,
but a non-grammar backend can still emit a reply that violates a *cross-field* /
*cardinality* rule the grammar cannot express. The fix is the loop, not the model:
re-ask with the precise error so the next attempt is corrected by code, and stop
deterministically.

**Transport is injected, not imported.** The loop never calls the network itself —
it takes a ``chat`` transport (an async callable) and splats the constrained-emission
kwargs through it via ``extra_body``. Any auth / vessel-stamp / Zero-Trust egress
policy lives in *that* transport, supplied by the caller (the orchestrator engine or
a vessel). This keeps the kernel standalone and secret-free: it depends on pydantic +
stdlib only, and on the shape of the transport — never on a specific app's router.

The ``chat`` transport contract (async)::

    async def chat(*, messages: list[dict],
                   model: str | None,
                   temperature: float,
                   extra_body: dict) -> dict:   # an OpenAI-style chat-completion response
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Sequence, Type

from pydantic import BaseModel

from .case_spec import TaskSpec, prompt_block_from_schema, task_spec_json_schema
from .constrained_emission import (
    ConstrainedEmissionError,
    emit_constrained,
    tool_emission_kwargs,
)
from .failure_class import FailureClass, classify_emission_failure
from .strategy import Strategy

logger = logging.getLogger(__name__)

#: Default number of emission attempts (1 emit + up to N-1 re-asks).
DEFAULT_RETRY_BUDGET = 3
#: Hard ceiling on attempts — a bounded loop is the whole point (cost / latency
#: guard). A caller asking for more is clamped, never honoured unbounded.
MAX_RETRY_BUDGET = 3

#: A semantic validator: receives the typed spec instance and raises (any
#: ``Exception`` with a human-readable message) if a rule the JSON-Schema cannot
#: express is violated — cross-field invariants, array cardinality. Must be PURE and
#: synchronous (no I/O); it runs inside the re-ask loop.
Validator = Callable[[BaseModel], None]

#: The injected async chat transport (see module docstring for the contract). It
#: receives the constrained-emission kwargs via ``extra_body`` and returns an
#: OpenAI-style chat-completion response dict.
ChatTransport = Callable[..., Awaitable[dict[str, Any]]]


class ValidatedEmitError(Exception):
    """Re-ask budget exhausted — the typed terminal failure (for callers that prefer
    raising over branching on :class:`ValidatedEmitResult`). Carries the last exact
    validator detail and the number of attempts spent."""

    def __init__(self, message: str, *, attempts: int, last_detail: Optional[str]) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.last_detail = last_detail


@dataclass(frozen=True)
class ValidatedEmitResult:
    """Deterministic outcome of a validated emission.

    ``ok=True`` → ``value`` is the validated, typed spec instance (and every semantic
    validator passed). ``ok=False`` → the budget was spent; ``value`` is ``None`` and
    ``detail`` carries the LAST exact validator error. ``reasks`` is the ordered list
    of error details fed back to the model (empty on a first-try success), useful for
    audit / observability.

    ``failure_class`` is an ADDITIVE, advisory label (#602 S602-3): on a terminal
    failure it carries the :class:`~ack.failure_class.FailureClass` derived
    deterministically from the last error; it is ``None`` on success (and on any
    result built without it — byte-identical to the pre-#602 shape). It never affects
    control flow — it is read only by the opt-in reflection layer.
    """

    ok: bool
    value: Optional[BaseModel]
    attempts: int
    error: Optional[str] = None
    detail: Optional[str] = None
    reasks: tuple[str, ...] = field(default_factory=tuple)
    # ``compare=False`` keeps ``__eq__``/``__hash__`` byte-identical to the pre-#602
    # shape (an advisory label, deterministically derived from ``detail``, must not
    # change result identity); it stays in ``repr`` on purpose — observability is the
    # point of the field.
    failure_class: Optional[FailureClass] = field(default=None, compare=False)

    def raise_for_status(self) -> BaseModel:
        """Return ``value`` on success, else raise :class:`ValidatedEmitError`."""
        if self.ok and self.value is not None:
            return self.value
        raise ValidatedEmitError(
            self.error or "validated emission failed",
            attempts=self.attempts,
            last_detail=self.detail,
        )


def _reask_message(detail: str, hint: str = "") -> dict[str, str]:
    """Build the re-ask turn that feeds the model the EXACT validator error.

    Deliberately surgical: "fix exactly these problems, change nothing else" keeps the
    next attempt from drifting on the parts that were already correct. ``hint`` is an
    OPTIONAL strategy instruction (#602 S602-7) appended verbatim; empty ⇒ the message
    is byte-identical to the pre-#602 re-ask."""
    content = (
        "Your previous output was REJECTED by the schema / semantic validator. "
        "Re-emit the FULL object via the tool, fixing EXACTLY these problems and "
        "changing nothing else:\n"
        f"{detail}"
    )
    if hint:
        content += f"\n\nStrategy: {hint}"
    return {"role": "user", "content": content}


def _run_validators(instance: BaseModel, validators: Optional[Sequence[Validator]]) -> None:
    """Run extra semantic validators, normalising any failure to a
    :class:`ConstrainedEmissionError` whose ``detail`` is the exact message — so a
    semantic rejection re-asks identically to a Pydantic rejection."""
    if not validators:
        return
    for validator in validators:
        try:
            validator(instance)
        except ConstrainedEmissionError:
            raise
        except Exception as exc:  # any validator error → exact-detail reask
            name = getattr(validator, "__name__", repr(validator))
            raise ConstrainedEmissionError(
                f"semantic validator {name!r} rejected the emission",
                detail=str(exc),
            ) from exc


async def emit_validated(
    spec_cls: Type[BaseModel],
    *,
    chat: ChatTransport,
    messages: list[dict[str, Any]],
    tool_name: str,
    validators: Optional[Sequence[Validator]] = None,
    budget: int = DEFAULT_RETRY_BUDGET,
    model: Optional[str] = None,
    temperature: float = 0.0,
    description: Optional[str] = None,
    force: bool = True,
    chat_template_kwargs: Optional[dict[str, Any]] = None,
    strategist: Optional[Callable[[FailureClass, int, int], Strategy]] = None,
) -> ValidatedEmitResult:
    """Emit a constrained, validated *spec_cls* instance with bounded re-ask retry.

    The loop: build the constrained request (:func:`tool_emission_kwargs`), POST it
    through the injected ``chat`` transport (which owns auth/vessel/egress), parse +
    validate the reply (:func:`emit_constrained` → the Pydantic floor) and run any
    extra ``validators`` (the semantic floor). On a syntax OR semantic rejection,
    append the EXACT error as a re-ask turn and retry, up to ``budget`` attempts
    (hard-capped at :data:`MAX_RETRY_BUDGET`). When the budget is spent, return a
    typed terminal failure (``ok=False``) — never a partially-valid object.

    ``messages`` is the seed conversation (system + user); it is copied, never
    mutated. ``temperature`` defaults to ``0.0`` for deterministic structured
    emission. Whatever the ``chat`` transport raises (transport / auth errors) is NOT
    the model's fault and propagates unchanged — it is not re-asked.

    ``strategist`` (#602 S602-7) is an OPTIONAL pure policy ``(failure_class, attempt,
    budget) → Strategy`` (e.g. :func:`ack.strategy.revise`): on a rejection its
    ``reask_hint`` is appended to the re-ask turn so the next attempt is targeted at the
    failure CLASS, not just the raw error. ``None`` (the default) ⇒ the re-ask is
    byte-identical to the pre-#602 loop. A strategist error is swallowed (advisory).
    """
    budget = max(1, min(int(budget), MAX_RETRY_BUDGET))
    emission_kwargs = tool_emission_kwargs(
        spec_cls, tool_name=tool_name, description=description, force=force,
        chat_template_kwargs=chat_template_kwargs,
    )

    conversation: list[dict[str, Any]] = list(messages)
    reasks: list[str] = []
    last_detail: Optional[str] = None
    last_message: Optional[str] = None
    last_failure: Optional[FailureClass] = None

    for attempt in range(1, budget + 1):
        # Transport / auth errors propagate: a transport that is off or upstream-
        # broken is not a model-output problem.
        response = await chat(
            messages=conversation,
            model=model,
            temperature=temperature,
            extra_body=emission_kwargs,
        )
        try:
            instance = emit_constrained(spec_cls, response, tool_name=tool_name)
            _run_validators(instance, validators)
        except ConstrainedEmissionError as exc:
            last_detail = exc.detail
            last_message = str(exc)
            last_failure = classify_emission_failure(last_message, last_detail)
            reasks.append(last_detail)
            # `detail` carries field names / rule messages, never prompt content —
            # safe to log for observability of the re-ask loop.
            logger.info(
                "validated-emit %s attempt %d/%d rejected (%s): %s",
                spec_cls.__name__, attempt, budget, last_failure.value, last_detail,
            )
            if attempt < budget:
                # #602 S602-7: an optional strategist turns the failure CLASS into a targeted re-ask
                # hint; default (None) ⇒ empty hint ⇒ byte-identical to the pre-#602 re-ask.
                hint = ""
                if strategist is not None:
                    try:
                        # str(...) INSIDE the guard so an odd reask_hint (non-str / raising __str__) is
                        # swallowed here, never later in _reask_message — advisory, never breaks the loop.
                        hint = str(strategist(last_failure, attempt, budget).reask_hint or "")
                    except Exception:   # noqa: BLE001 — advisory: a strategist error never breaks the loop
                        hint = ""
                conversation = [*conversation, _reask_message(last_detail, hint)]
            continue
        return ValidatedEmitResult(
            ok=True, value=instance, attempts=attempt, reasks=tuple(reasks)
        )

    # Budget spent → deterministic typed terminal failure.
    logger.warning(
        "validated-emit %s failed after %d attempt(s) — typed failure",
        spec_cls.__name__, budget,
    )
    return ValidatedEmitResult(
        ok=False,
        value=None,
        attempts=budget,
        error=f"validated emission of {spec_cls.__name__} failed after {budget} attempt(s)",
        detail=last_detail,
        reasks=tuple(reasks),
        # Advisory, deterministic (#602 S602-3) — the class of the last error; never
        # alters control flow. last_failure is always set here (the loop only falls
        # through after at least one ConstrainedEmissionError classified it).
        failure_class=last_failure or classify_emission_failure(last_message or "", last_detail),
    )


# --------------------------------------------------------------------------- #
# Reusable semantic validators (rules XGrammar cannot carry — live here, not in
# the schema). See :mod:`ack.case_spec` on why array cardinality is banned from the
# grammar.
# --------------------------------------------------------------------------- #


def require_min_acceptance_criteria(minimum: int = 1) -> Validator:
    """A cardinality validator: at least *minimum* ``acceptance_criteria`` entries.

    ``minItems`` 400s under XGrammar V1, so this rule cannot live in the schema — it
    is enforced here, inside the re-ask loop.
    """

    def _validator(spec: BaseModel) -> None:
        crit = getattr(spec, "acceptance_criteria", None) or []
        if len(crit) < minimum:
            raise ValueError(
                f"acceptance_criteria must have at least {minimum} entr"
                f"{'y' if minimum == 1 else 'ies'} (got {len(crit)})"
            )

    _validator.__name__ = f"require_min_acceptance_criteria_{minimum}"
    return _validator


# --------------------------------------------------------------------------- #
# Convenience entry — emit a task spec through the bounded loop.
# --------------------------------------------------------------------------- #

#: Tool name the model is forced to call when emitting a ``task_json``.
EMIT_TASK_TOOL = "emit_task_json"

_TASK_EMITTER_SYSTEM_PROMPT = (
    "You are a deterministic task-spec emitter inside an orchestration pipeline. "
    "Emit EXACTLY ONE task by calling the provided tool with arguments that satisfy "
    "the contract. Never add keys, never omit a required key."
)


def _task_seed_messages(
    instruction: str,
    spec_cls: Type[BaseModel],
    extra_rules: Optional[Sequence[str]] = None,
) -> list[dict[str, Any]]:
    """Seed conversation for a task emission: role-lock + schema block + the ask. The
    schema block is derived from *spec_cls* (so it works for the base TaskSpec or any
    subclass, e.g. Lodestar's CapabilityTaskSpec). ``extra_rules`` lets a plugin
    advertise domain hard rules (e.g. Lodestar's mandatory ``capability``)."""
    schema_block = prompt_block_from_schema(
        task_spec_json_schema(spec_cls), extra_rules=extra_rules
    )
    return [
        {"role": "system", "content": _TASK_EMITTER_SYSTEM_PROMPT},
        {"role": "user", "content": f"{schema_block}\n\nTask to emit:\n{instruction}"},
    ]


async def emit_task_spec(
    *,
    chat: ChatTransport,
    instruction: str,
    spec_cls: Type[BaseModel] = TaskSpec,
    validators: Optional[Sequence[Validator]] = None,
    extra_prompt_rules: Optional[Sequence[str]] = None,
    budget: int = DEFAULT_RETRY_BUDGET,
    model: Optional[str] = None,
    chat_template_kwargs: Optional[dict[str, Any]] = None,
) -> ValidatedEmitResult:
    """Emit a validated ``task_json`` via the bounded re-ask loop.

    Defaults to the generic :class:`~ack.case_spec.TaskSpec`. When the Lodestar
    plugin is enabled, pass ``spec_cls=CapabilityTaskSpec`` and
    ``extra_prompt_rules=[capability_prompt_rule()]`` so the "forgot capability"
    failure becomes impossible through this path (the spec's validator enforces it,
    and the prompt advertises it). ``instruction`` is the natural-language
    description of the task to create.
    """
    return await emit_validated(
        spec_cls,
        chat=chat,
        messages=_task_seed_messages(instruction, spec_cls, extra_rules=extra_prompt_rules),
        tool_name=EMIT_TASK_TOOL,
        validators=validators,
        budget=budget,
        model=model,
        chat_template_kwargs=chat_template_kwargs,
    )
