"""Failure Classification — shared taxonomy (Agent-Contract-Kernel, #602 S602-3).

> **One vocabulary for *why* an agent step failed.** Ironclad already classifies a
> code-agent's RAW run result into three buckets (:mod:`engine.providers`
> ``classify_agent_result``) and re-asks a rejected emission with the exact validator
> error (:mod:`ack.validated_emit`). What it lacked was a *single* enum that names the
> failure mode so every reflection consumer — the validated-emit loop, the code-agent
> breaker, and (engine-side) the Strategy Revisor (#602 SUB-7) — speaks one SSOT.

:class:`FailureClass` is that enum. It is **advisory, not the contract-SSOT**: a class
is attached to an already-decided outcome (a spent re-ask budget, a dead agent). It
*never* relaxes a gate, never alters control flow on the fail-closed path — it is read
only by the additive, opt-in reflection layer.

**Deterministic on the ACK side.** :func:`classify_emission_failure` is pure
rule-based string matching over the validator/emission error the re-ask loop already
captures — no model, no I/O, offline-testable, never raises. Any *model-based*
classification (e.g. labelling a free-text agent answer) lives engine-side and still
lands on this same enum, so the taxonomy stays single-sourced.

This module imports only the stdlib (``enum`` + ``typing``) — it is a leaf of the
kernel and standalone-importable.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional, Tuple


class FailureClass(str, Enum):
    """Why an agent step failed — the shared reflection vocabulary (#602).

    A ``str`` enum so a value serialises as its plain string (JSON / logs / lesson
    metadata) without a custom encoder. The members are the union of what the
    deterministic ACK classifier can observe today and what engine-side classification
    will produce; each member's producer is named so none is decorative:

    - ``MISSING_CONTEXT`` — the step lacked information it needed (engine-side).
    - ``BAD_TOOL_ARGS`` — the model failed to produce a usable call to the forced tool
      (no/wrong call, malformed envelope) — :func:`classify_emission_failure`.
    - ``RETRIEVAL_FAILURE`` — a retrieval/grounding lookup returned nothing usable
      (engine-side; the Verifier's grounding check, #602 SUB-4).
    - ``HALLUCINATED_ASSUMPTION`` — the output asserted something unsupported
      (engine-side, model-judged).
    - ``INCOMPLETE_OUTPUT`` — the output has the right shape but a REQUIRED piece is
      missing/empty or a cardinality floor is unmet (the model *under-filled* a valid
      object) — :func:`classify_emission_failure`; also the code-agent ``task-failed``
      result (:func:`~engine.providers.result_failure_class`).
    - ``POLICY_CONFLICT`` — the request collided with a policy/permission rule
      (engine-side).
    - ``SCHEMA_INVALID`` — an unparseable payload OR a wrong-shape/wrong-type/extra-key
      violation (the model emitted the *wrong shape*) — :func:`classify_emission_failure`
      (also the conservative default).
    - ``UNAVAILABLE`` — a backend was budget/quota exhausted or unreachable — the
      code-agent ``agent-unavailable`` result (:func:`~engine.providers.result_failure_class`).
    """

    MISSING_CONTEXT = "missing_context"
    BAD_TOOL_ARGS = "bad_tool_args"
    RETRIEVAL_FAILURE = "retrieval_failure"
    HALLUCINATED_ASSUMPTION = "hallucinated_assumption"
    INCOMPLETE_OUTPUT = "incomplete_output"
    POLICY_CONFLICT = "policy_conflict"
    SCHEMA_INVALID = "schema_invalid"
    UNAVAILABLE = "unavailable"


#: Ordered, most-specific-first rules over the lowercased error text. The FIRST rule
#: with any of its needles present wins. The text is the validated-emit failure's
#: ``message`` + ``detail`` — exactly what the re-ask loop already records. Anchored on
#: phrases the emission/validation layers actually emit (see :mod:`ack.constrained_emission`
#: and :mod:`ack.validated_emit`), not on bare tokens — so the ``"tool_call.arguments:"``
#: prefix of a *parse* failure does not get mistaken for a call-envelope failure.
_EMISSION_RULES: Tuple[Tuple[Tuple[str, ...], FailureClass], ...] = (
    # CALL ENVELOPE — the model failed to produce a usable call to the forced tool.
    (("no tool_call for", "neither tool_calls nor content", "missing choices",
      "choices[0].message"), FailureClass.BAD_TOOL_ARGS),
    # UNDER-FILLED — the shape is right but a REQUIRED piece is missing/empty or a
    # cardinality floor is unmet: incomplete, not malformed. Checked BEFORE the schema
    # rule because EVERY Pydantic error string also contains "validation" (so a missing
    # field / a "...should have at least N items" error must be caught here first).
    # Needles are the actual strings emitted by Pydantic ("Field required",
    # "[type=missing]") and the kernel's own validators (case_spec "must be a non-empty
    # string" / "must not contain empty entries"; lodestar "'capability' is mandatory
    # for type=...") + cardinality phrasings.
    (("field required", "[type=missing]", "is mandatory for", "non-empty", "empty entr",
      "at least", "too few", "fewer than"), FailureClass.INCOMPLETE_OUTPUT),
    # WRONG SHAPE / PARSE — an unparseable payload OR a wrong-type / wrong-value /
    # extra-key violation. NB: "value_error" is deliberately NOT a needle here — the
    # under-filled custom validators above are themselves type=value_error; a remaining
    # value_error (e.g. a bad Task-ID / ISO-8601 format) is a wrong VALUE and is caught
    # by the "validation" catch-all.
    (("not parseable json", "not a json object", "expected json object",
      "input should be", "extra inputs", "not permitted", "validation"),
     FailureClass.SCHEMA_INVALID),
)


def classify_emission_failure(message: str, detail: Optional[str] = None) -> FailureClass:
    """Deterministically map a validated-emit failure to a :class:`FailureClass`.

    Pure, rule-based over the lowercased ``message`` + ``detail`` (what the re-ask loop
    already has in hand). Conservative: an unrecognised emission failure is
    :attr:`SCHEMA_INVALID` — the re-ask loop only ever fails on a schema/semantic
    rejection (a transport/auth error propagates *before* this is reached), so the
    default names the honest class. Never raises.
    """
    try:
        text = f"{message or ''}\n{detail or ''}".lower()
        for needles, klass in _EMISSION_RULES:
            if any(n in text for n in needles):
                return klass
        return FailureClass.SCHEMA_INVALID
    except Exception:   # noqa: BLE001 — absolute never-raises (a hostile message/detail dunder) → the
        return FailureClass.SCHEMA_INVALID   # conservative default; symmetric with strategy.revise
