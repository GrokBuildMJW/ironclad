"""Constrained Emission (Hard-Floor) — Agent-Contract-Kernel component 2.

> **The hard floor.** Component 1 (:mod:`ack.case_spec`) owns the SSOT
> schema; this module turns that one schema into a *grammar-constrained emission
> request* and parses/validates the model's structured reply **fail-closed**, so a
> required field is impossible to silently drop. It is the seam the Validated-Emit
> retry loop (component 3, next task) wraps with a re-ask budget.

Why this exists (ACK MVP, design doc §"Constrained Emission (Hard-Floor)"):
required fields must be *token-physically* unavoidable, model- and quant-agnostic.
Two layers deliver that, and BOTH come from the same ``model_json_schema()`` so
prompt / grammar / validator can never drift:

  1. **Request constraint (grammar floor).** On a vLLM backend, a tool whose
     ``parameters`` is the closed schema (``required`` + ``additionalProperties:
     false``) plus ``tool_choice="required"`` makes XGrammar logit-mask the decode:
     the model *cannot* emit a missing required key or a junk key (verified:
     vLLM tool-calling guarantees the args "conform to the function's parameter
     schema"). This is quant-compatible (logit-masking, not training).
  2. **Runtime re-validation floor.** The reply is re-validated against the Pydantic
     spec regardless of backend. This is the ONLY floor that holds on a backend
     WITHOUT grammar masking (e.g. a model/runtime that cannot logit-mask, or the
     parked GB10 grammar path). So re-validation is not belt-and-suspenders, it is
     load-bearing whenever the grammar layer is absent.

**Transport boundary.** This module is deliberately transport-agnostic: it BUILDS
request kwargs and PARSES responses, but never calls the network itself. The caller
(the Validated-Emit loop, component 3) splats these kwargs through an injected chat
transport — so any auth/vessel/egress policy lives in that transport, not here.
Keeping emission pure means it imports with only pydantic + stdlib and is fully
unit-testable without a running model.

**vLLM API (verified 2026-06-16, v0.12.0+).** The legacy ``guided_*`` sampling
fields were removed in v0.12.0. Over the *HTTP / OpenAI-compatible* API (what
LiteLLM speaks) the constraint travels as either ``tools`` + ``tool_choice``
(preferred — gives a typed tool-call) or ``response_format={"type":"json_schema",
...}`` (bare JSON object). The ``structured_outputs={...}`` form
(:func:`ack.case_spec.vllm_structured_output_config`) is the *offline*
``SamplingParams`` shape; it is retained for the offline path and NOT what the
router POSTs. See :func:`tool_emission_kwargs` / :func:`json_schema_response_format`.
"""
from __future__ import annotations

import json
from typing import Any, Optional, Type, Union

from pydantic import BaseModel, ValidationError

from .case_spec import lint_schema_for_xgrammar

#: Accept either a Pydantic-v2 model class (preferred — keeps the SSOT) or an
#: already-derived JSON-Schema dict (for ad-hoc / non-Pydantic cases).
SpecSource = Union[Type[BaseModel], dict[str, Any]]

#: Chat-template kwargs that disable a reasoning model's thinking for the current
#: turn. **Critical for deterministic emission on reasoning models** (e.g. Qwen3.x):
#: with thinking ON, the model emits reasoning prose that pollutes/truncates the
#: structured output (measured: 33–67% valid); with it OFF the constrained emission
#: is grammar-clean (measured: 100% valid, ~12x faster). Pass via ``chat_template_kwargs``.
#: NOTE the correct key is ``enable_thinking`` (current turn), NOT ``preserve_thinking``
#: (only affects history). Backend/template-specific (vLLM honours it for Qwen3.x).
THINKING_OFF: dict[str, Any] = {"enable_thinking": False}


class ConstrainedEmissionError(Exception):
    """A constrained emission could not be parsed/validated — typed, fail-closed.

    Carries the *exact* offending detail (e.g. the Pydantic error string). The
    Validated-Emit loop (component 3) feeds ``detail`` back to the model verbatim
    on a bounded re-ask; after the budget is spent this same type is the
    deterministic terminal failure signal.
    """

    def __init__(self, message: str, *, detail: Optional[str] = None) -> None:
        super().__init__(message)
        self.detail = detail or message


# --------------------------------------------------------------------------- #
# Schema extraction + grammar-safety guard
# --------------------------------------------------------------------------- #


def _schema_of(source: SpecSource) -> dict[str, Any]:
    """Return the JSON-Schema for *source* (a Pydantic model class or raw schema)."""
    if isinstance(source, dict):
        return source
    if isinstance(source, type) and issubclass(source, BaseModel):
        return source.model_json_schema()
    raise TypeError(
        "constrained-emission source must be a Pydantic BaseModel subclass or a "
        f"JSON-Schema dict, got {type(source).__name__}"
    )


def _assert_grammar_safe(schema: dict[str, Any]) -> None:
    """Fail LOUD before egress if *schema* carries XGrammar-unsupported keywords.

    XGrammar V1 *400s* on array-cardinality keywords (``minItems`` …). Catching it
    here turns a remote 400 into a precise local error naming the field+keyword, so
    cardinality rules get moved into a validator (component 3) instead.
    """
    findings = lint_schema_for_xgrammar(schema)
    if findings:
        raise ConstrainedEmissionError(
            "schema is not XGrammar-safe; move these rules into validators: "
            + "; ".join(str(f) for f in findings),
            detail="; ".join(str(f) for f in findings),
        )


# --------------------------------------------------------------------------- #
# (1·grammar floor) build the constrained request
# --------------------------------------------------------------------------- #


def build_function_tool(
    source: SpecSource,
    *,
    name: str,
    description: Optional[str] = None,
) -> dict[str, Any]:
    """Build one OpenAI/vLLM *function tool* descriptor from the SSOT schema.

    ``parameters`` is the closed schema (``required`` + ``additionalProperties:
    false`` as produced by :class:`~ack.case_spec.TaskSpec`), which is what
    XGrammar masks against. Asserts grammar-safety first (fail-loud).
    """
    schema = _schema_of(source)
    _assert_grammar_safe(schema)
    fn: dict[str, Any] = {"name": name, "parameters": schema}
    if description:
        fn["description"] = description
    return {"type": "function", "function": fn}


def tool_emission_kwargs(
    source: SpecSource,
    *,
    tool_name: str,
    description: Optional[str] = None,
    force: bool = True,
    chat_template_kwargs: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Request kwargs that constrain the model to emit ONE call to *tool_name*.

    Returns ``{"tools": [<fn tool>], "tool_choice": ...}`` to splat into the
    chat/completions body. With ``force=True`` (default) ``tool_choice`` *names*
    the tool — the strongest floor: the model must call exactly this function and
    its arguments must conform to the parameter schema (required keys unavoidable).
    ``force=False`` uses ``"required"`` (must call *some* tool) for the
    multi-tool / agentic case where the model picks from an allow-list.

    ``chat_template_kwargs`` is passed through to the request (backend-specific). On
    a **reasoning model**, pass :data:`THINKING_OFF` here — deterministic emission
    must not think (otherwise reasoning prose pollutes the structured args). The
    transport (e.g. ``ai_router``/OpenAI client ``extra_body``) forwards it verbatim.
    """
    tool = build_function_tool(source, name=tool_name, description=description)
    tool_choice: Any = (
        {"type": "function", "function": {"name": tool_name}} if force else "required"
    )
    kwargs: dict[str, Any] = {"tools": [tool], "tool_choice": tool_choice}
    if chat_template_kwargs:
        kwargs["chat_template_kwargs"] = chat_template_kwargs
    return kwargs


def json_schema_response_format(
    source: SpecSource,
    *,
    name: str,
    strict: bool = False,
) -> dict[str, Any]:
    """Request kwargs for bare-JSON-object constraint over the HTTP API.

    Returns ``{"response_format": {"type": "json_schema", "json_schema": {...}}}``
    — the OpenAI-compatible shape vLLM v0.12.0+ accepts over HTTP (NOT the offline
    ``structured_outputs`` SamplingParams form). Use this when a tool-call wrapper
    is undesirable and a raw JSON object is wanted.

    ``strict`` defaults to **False** on purpose: OpenAI strict mode requires every
    property to be ``required`` and every object closed, which rejects specs with
    optional fields (e.g. :class:`TaskSpec`). vLLM enforces the ``required`` array
    via XGrammar regardless of ``strict``, so the hard floor does not need it.
    """
    schema = _schema_of(source)
    _assert_grammar_safe(schema)
    js: dict[str, Any] = {"name": name, "schema": schema}
    if strict:
        js["strict"] = True
    return {"response_format": {"type": "json_schema", "json_schema": js}}


# --------------------------------------------------------------------------- #
# (2·runtime floor) parse + validate the structured reply, fail-closed
# --------------------------------------------------------------------------- #


def _loads_object(raw: Any, *, where: str) -> dict[str, Any]:
    """Parse *raw* (already a dict, or a JSON string) into a dict, fail-closed.

    Tolerates a stray ```` ```json … ``` ```` fence (some non-grammar backends add
    one) but nothing else — a non-object or unparseable payload raises.
    """
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        raise ConstrainedEmissionError(f"{where}: expected JSON object, got {type(raw).__name__}")
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        obj = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise ConstrainedEmissionError(f"{where}: reply is not parseable JSON", detail=str(exc)) from exc
    if not isinstance(obj, dict):
        raise ConstrainedEmissionError(f"{where}: reply is not a JSON object")
    return obj


def extract_tool_call(
    response: dict[str, Any],
    *,
    tool_name: Optional[str] = None,
) -> dict[str, Any]:
    """Pull the tool-call arguments out of a chat-completion *response*, fail-closed.

    Prefers the native ``choices[0].message.tool_calls`` path (grammar backends).
    Falls back to parsing ``message.content`` as a JSON object when the backend
    cannot emit native tool-calls — so the same call site works on both. When *tool_name* is given, the first matching
    call is selected; otherwise the first call. Returns the raw argument dict (NOT
    yet semantically validated — that is :func:`emit_constrained`).
    """
    try:
        message = response["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ConstrainedEmissionError("response missing choices[0].message", detail=str(exc)) from exc

    tool_calls = message.get("tool_calls") if isinstance(message, dict) else None
    if tool_calls:
        chosen = None
        for call in tool_calls:
            # #1558: a malformed element (a non-dict, or {"function": null}) must not crash the parser with
            # an AttributeError that escapes emit_validated's retry — it is simply not a usable tool call.
            fn = call.get("function") if isinstance(call, dict) else None
            if not isinstance(fn, dict):
                continue
            if tool_name is None or fn.get("name") == tool_name:
                chosen = fn
                break
        if chosen is None:
            names = [c.get("function", {}).get("name")
                     for c in tool_calls
                     if isinstance(c, dict) and isinstance(c.get("function"), dict)]
            raise ConstrainedEmissionError(
                f"no tool_call for {tool_name!r} in response (got {names})"
            )
        return _loads_object(chosen.get("arguments"), where="tool_call.arguments")

    # Fallback: a backend without native tool-calls returned the object as content.
    content = message.get("content") if isinstance(message, dict) else None
    if content is None:
        raise ConstrainedEmissionError("response has neither tool_calls nor content")
    return _loads_object(content, where="message.content")


def emit_constrained(
    spec_cls: Type[BaseModel],
    response: dict[str, Any],
    *,
    tool_name: Optional[str] = None,
) -> BaseModel:
    """Extract + **validate** a constrained reply into a typed *spec_cls* instance.

    This is the hard floor's runtime half: even if the grammar layer is absent
    (non-vLLM backend), a reply that omits a required field or violates a
    cross-field rule is rejected here with the EXACT Pydantic error — which is what
    component 3 re-asks the model with. Raises :class:`ConstrainedEmissionError`
    on a malformed transport reply OR a spec violation.
    """
    args = extract_tool_call(response, tool_name=tool_name)
    try:
        return spec_cls.model_validate(args)
    except ValidationError as exc:
        raise ConstrainedEmissionError(
            f"constrained reply failed {spec_cls.__name__} validation",
            detail=str(exc),
        ) from exc


# --------------------------------------------------------------------------- #
# Ops helper — recommended vLLM server launch flags (documentation/grounding)
# --------------------------------------------------------------------------- #

#: Tool-call parser per model family (per vLLM's tool-calling docs). This framework
#: targets Qwen3-Coder; for other families, pick the matching value from vLLM's
#: ``--tool-call-parser`` options.
_TOOL_CALL_PARSER: dict[str, str] = {
    "qwen3-coder": "qwen3_coder",
    "qwen3_coder": "qwen3_coder",
}


def recommended_vllm_server_flags(model_family: str) -> list[str]:
    """Return the vLLM server flags that make *model_family* emit grammar-constrained
    tool-calls. Use to ground an ops runbook / a future vLLM service definition.

    Example: ``recommended_vllm_server_flags("qwen3-coder")`` →
    ``["--enable-auto-tool-choice", "--tool-call-parser", "qwen3_coder"]``.
    """
    parser = _TOOL_CALL_PARSER.get(model_family.strip().lower())
    if parser is None:
        raise ValueError(
            f"unknown model family {model_family!r}; known: {sorted(_TOOL_CALL_PARSER)}"
        )
    return ["--enable-auto-tool-choice", "--tool-call-parser", parser]
