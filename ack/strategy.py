"""Strategy Revisor — the pure failure→action policy (Agent-Contract-Kernel, #602 S602-7).

> **Stop retrying the same way.** When an attempt fails, the bounded re-ask loop (and the code-agent
> failover) currently just try *the same path again*. This module is the **deterministic policy** that
> turns a :class:`~ack.failure_class.FailureClass` into a *targeted* next move — inject context, narrow
> the call, switch retrieval, ground the claim, complete the output, repair the schema, fail over, or
> escalate to a human when the budget is spent.

:func:`revise` is **pure** (``(failure_class, attempt, budget) → Strategy``): no transport, no I/O, no
model — so it is snapshot-testable and the single SSOT both the ACK re-ask seam
(:func:`ack.validated_emit.emit_validated`'s optional ``strategist``) and the engine-side application
(:func:`engine.providers.code_agent_strategy` for the code-agent failover path) consume. The *effect* of
a :class:`Strategy` (actually injecting RAG, switching the retrieval source, failing over to another
agent) is the consumer's job — it needs engine I/O and cannot live in this pure kernel module.

Imports only the stdlib + :mod:`ack.failure_class`; standalone, byte-identical-default (nothing applies a
strategy unless a caller opts in by passing/using one).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .failure_class import FailureClass


class StrategyAction(str, Enum):
    """The targeted next move for a failed attempt — a ``str`` enum so it serialises plainly."""

    INJECT_CONTEXT = "inject_context"          # MISSING_CONTEXT → identify + incorporate the missing context
    NARROW_OR_CLARIFY = "narrow_or_clarify"    # BAD_TOOL_ARGS → constrain the call to the tool schema
    SWITCH_RETRIEVAL = "switch_retrieval"      # RETRIEVAL_FAILURE → broaden / rephrase / alternate source
    GROUND_THEN_ANSWER = "ground_then_answer"  # HALLUCINATED_ASSUMPTION → ground each claim, no assumptions
    COMPLETE_OUTPUT = "complete_output"        # INCOMPLETE_OUTPUT → supply the missing/under-filled parts
    RESOLVE_POLICY = "resolve_policy"          # POLICY_CONFLICT → narrow scope to what is allowed
    REPAIR_SCHEMA = "repair_schema"            # SCHEMA_INVALID → re-emit conforming to the schema
    FAIL_OVER = "fail_over"                    # UNAVAILABLE → switch to another agent/backend (engine action)
    HUMAN_ESCALATION = "human_escalation"      # terminal (budget spent) → hand off to a human
    RETRY_SAME = "retry_same"                  # default fallback → the pre-#602 behaviour


@dataclass(frozen=True)
class Strategy:
    """The revisor's verdict for one failed attempt.

    ``action`` is the targeted move; ``reask_hint`` is the short, deterministic instruction a re-ask
    consumer appends to the next attempt (``""`` for an engine-only action like fail-over or a terminal
    escalation — there is no productive model instruction to add); ``escalate`` is ``True`` only for the
    terminal hand-off (no productive retry remains).
    """

    action: StrategyAction
    reask_hint: str = ""
    escalate: bool = False


#: FailureClass → (action, reask_hint). The hint is a generic, model-agnostic instruction (no task content)
#: so the policy stays pure + snapshot-stable. UNAVAILABLE maps to an ENGINE action (fail over) with no
#: model hint; an unrecognised class falls back to the pre-#602 "retry the same way".
_POLICY: dict = {
    FailureClass.MISSING_CONTEXT: (
        StrategyAction.INJECT_CONTEXT,
        "State exactly what information was missing and incorporate the provided context before re-emitting.",
    ),
    FailureClass.BAD_TOOL_ARGS: (
        StrategyAction.NARROW_OR_CLARIFY,
        "Call the required tool with arguments that conform exactly to its schema; do not free-form the reply.",
    ),
    FailureClass.RETRIEVAL_FAILURE: (
        StrategyAction.SWITCH_RETRIEVAL,
        "The previous retrieval returned nothing usable; broaden or rephrase the query, or use another source.",
    ),
    FailureClass.HALLUCINATED_ASSUMPTION: (
        StrategyAction.GROUND_THEN_ANSWER,
        "Do not assume unstated facts; ground every claim in the provided material or state the uncertainty.",
    ),
    FailureClass.INCOMPLETE_OUTPUT: (
        StrategyAction.COMPLETE_OUTPUT,
        "Your output was missing required content; provide every required field/section, none empty.",
    ),
    FailureClass.POLICY_CONFLICT: (
        StrategyAction.RESOLVE_POLICY,
        "The request conflicts with a policy or permission; narrow the scope to what is allowed.",
    ),
    FailureClass.SCHEMA_INVALID: (
        StrategyAction.REPAIR_SCHEMA,
        "Re-emit strictly conforming to the schema; fix the type/shape error and change nothing else.",
    ),
    FailureClass.UNAVAILABLE: (
        StrategyAction.FAIL_OVER,
        "",   # engine action (switch backend/agent) — no productive model instruction
    ),
}


def revise(failure_class: FailureClass, attempt: int, budget: int) -> Strategy:
    """Map a :class:`FailureClass` (and where we are in the budget) to a targeted :class:`Strategy`.

    Pure + deterministic. When the budget is spent (``attempt >= budget``) the verdict is the terminal
    :attr:`StrategyAction.HUMAN_ESCALATION` (no productive retry remains) regardless of class. Otherwise
    the class drives the action via :data:`_POLICY`; an unrecognised class falls back to
    :attr:`StrategyAction.RETRY_SAME` (the pre-#602 behaviour). Never raises.
    """
    # Absolute "never raises" contract: an OUTER guard backstops anything pathological (e.g. a hostile
    # __class__ that makes isinstance raise) → a safe RETRY_SAME. The INNER numeric guard is kept so a
    # merely-odd attempt/budget (e.g. a raising __int__) still yields the TARGETED action, not the fallback.
    try:
        try:
            spent = int(attempt) >= int(budget)
        except Exception:   # noqa: BLE001 — TypeError/ValueError/OverflowError or a hostile __int__
            spent = False
        if spent:
            return Strategy(StrategyAction.HUMAN_ESCALATION, "", escalate=True)
        # A non-FailureClass / unhashable input would make dict.get raise → fall back to the pre-#602
        # RETRY_SAME for an unknown class.
        if not isinstance(failure_class, FailureClass):
            return Strategy(StrategyAction.RETRY_SAME, "")
        action, hint = _POLICY.get(failure_class, (StrategyAction.RETRY_SAME, ""))
        return Strategy(action, hint)
    except Exception:   # noqa: BLE001 — pathological input (e.g. a raising __class__) → safe default
        return Strategy(StrategyAction.RETRY_SAME, "")
