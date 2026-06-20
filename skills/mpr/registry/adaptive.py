"""Adaptive panel generation for an unknown domain (Spec 05 §7.5).

When ``resolve(domain)`` misses or the router picks ``domain=adhoc``: decompose the question into a
question-specific panel of ``MIN_ROLES..MAX_ROLES`` DISTINCT lenses — **never** a hardcoded universal
5-role set (Spec 02 §12 🔴). The decomposition rides ironclad's ``emit_validated`` (validate→reask→retry,
budget hard-capped at 3) — no own re-ask loop, no direct network call; the injected ``chat`` transport
owns auth/vessel/egress. The distinctness guard is passed as a *validator* so a clone-panel re-asks
exactly like a schema violation.

Fail-soft (Spec 05 §11): if the emit fails (``ok=False``) OR the transport raises, fall back to the
nearest known panel as a scaffold — NEVER to a hardcoded set. If there is no nearest panel either
(empty registry), raise ``AdhocGenerationError`` so the router degrades to *decline* (honest, never a
fabricated generic panel).
"""
from __future__ import annotations

import logging
from typing import Optional, Union

from ack.case_spec import prompt_block_from_schema  # ride the one schema→prompt derivation

from ack.validated_emit import emit_validated  # ride the bounded validate→reask loop

from .guards import _tokenize, check_distinctness
from .loader import PanelRegistry
from .schema import MAX_ROLES, MIN_ROLES, Mode, Panel, panel_json_schema

logger = logging.getLogger(__name__)

EMIT_PANEL_TOOL = "emit_panel"

_ADHOC_SYSTEM = (
    "You are MPR's adaptive panel generator inside an orchestration pipeline. Decompose the question "
    "into a panel of GENUINELY DISTINCT expert role lenses — each a different analytical angle, never "
    "a paraphrase of another. Emit EXACTLY ONE Panel object by calling the provided tool. Set "
    "domain='adhoc'. Every role needs a short role label and a lens_prompt (the instruction that lens "
    "uses to view the question). Choose a mode and synthesis_template that fit the question."
)


class AdhocGenerationError(RuntimeError):
    """Adhoc generation failed and there was no nearest panel to fall back to (router → decline)."""


def _distinctness_validator(panel: Panel) -> None:
    """Semantic validator for emit_validated: reject a panel whose roles are rephrasings."""
    findings = check_distinctness(panel)
    if findings:
        raise ValueError("; ".join(findings))


def _panel_signature(panel: Panel) -> set[str]:
    text = " ".join([panel.domain, panel.description, *(f"{r.role} {r.lens_prompt}" for r in panel.roles)])
    return _tokenize(text)


def nearest_panel(
    query: str,
    registry: Optional[PanelRegistry],
    *,
    hint_domain: Optional[str] = None,
) -> Optional[Panel]:
    """Pick the most similar known panel as a scaffold, or ``None``.

    ``hint_domain`` (the router's domain guess) takes precedence when resolvable. Otherwise score each
    known panel by stemmed-token overlap between the query and the panel's text (domain + description +
    role lenses) — deterministic, no LLM. Returns ``None`` when nothing overlaps (or no registry).
    """
    if registry is None:
        return None
    if hint_domain:
        hit = registry.resolve(hint_domain)
        if hit is not None:
            return hit
    q = _tokenize(query)
    if not q:
        return None
    best: Optional[Panel] = None
    best_score = 0
    for dom in registry.domains():
        panel = registry.resolve(dom)
        if panel is None:
            continue
        score = len(q & _panel_signature(panel))
        if score > best_score:
            best, best_score = panel, score
    return best if best_score > 0 else None


async def generate_adhoc_panel(
    query: str,
    *,
    chat,
    mode: Union[Mode, str] = Mode.EVIDENCE_RESEARCH,
    hint_domain: Optional[str] = None,
    registry: Optional[PanelRegistry] = None,
    budget: int = 3,
) -> Panel:
    """Generate a question-specific adhoc panel via ``emit_validated`` (Spec 05 §7.5).

    Hybrid: the nearest known panel (if any) seeds the prompt as a scaffold AND is the fail-soft
    fallback. The returned panel's ``domain`` is forced to ``'adhoc'``. Raises
    ``AdhocGenerationError`` only when generation fails *and* there is no scaffold to fall back to.
    """
    mode_value = mode.value if isinstance(mode, Mode) else str(mode)
    skeleton = nearest_panel(query, registry, hint_domain=hint_domain)

    schema_block = prompt_block_from_schema(
        panel_json_schema(),
        extra_rules=[
            "domain MUST be the literal string 'adhoc'.",
            f"Provide between {MIN_ROLES} and {MAX_ROLES} roles.",
            "Each role's lens_prompt must be a genuinely different analytical lens, not a rephrasing.",
        ],
    )
    hint = ""
    if skeleton is not None:
        labels = ", ".join(r.role for r in skeleton.roles)
        hint = (
            f"\n\nThe nearest known domain is {skeleton.domain!r}; you MAY use these lenses as a "
            f"starting scaffold and adapt / extend / drop them: {labels}."
        )
    user = f"{schema_block}{hint}\n\nMode: {mode_value}\nQuestion to decompose into a panel:\n{query}"
    messages = [
        {"role": "system", "content": _ADHOC_SYSTEM},
        {"role": "user", "content": user},
    ]

    result = None
    try:
        result = await emit_validated(
            Panel,
            chat=chat,
            messages=messages,
            tool_name=EMIT_PANEL_TOOL,
            validators=[_distinctness_validator],
            budget=budget,
            chat_template_kwargs={"enable_thinking": False},  # deterministic structured emission
        )
    except Exception as exc:  # noqa: BLE001 — a transport/auth throw must degrade fail-soft, not crash
        logger.warning("mpr-adaptive: transport error during adhoc emit: %s", exc)

    if result is not None and result.ok and result.value is not None:
        data = result.value.model_dump()
        data["domain"] = "adhoc"  # the defining property of an adhoc panel
        return Panel.model_validate(data)

    # Fail-soft: nearest known scaffold — NEVER a hardcoded universal set.
    if skeleton is not None:
        logger.warning(
            "mpr-adaptive: adhoc emit failed (%s); falling back to nearest panel %r",
            (result.detail if result is not None else "transport error"), skeleton.domain,
        )
        return skeleton
    raise AdhocGenerationError(
        "adhoc panel generation failed and no nearest panel to fall back to"
    )
