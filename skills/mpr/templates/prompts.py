"""Synthesis prompt contract (Spec 06 §5) — one shared system prompt + a per-mode user prompt.

English is the source/default; a ``<lang>.json`` overlay (``skills/mpr/locales``) supplies translations
of the system/mode/template guidance + labels (#18-4c) keyed under ``synthesis.*``. LLM-free prompt
*construction* only (the call itself is the injected ``llm_call`` in synthesis.py). The system prompt
forbids smoothing conflicts away (§5.1); the user prompt lays out the role opinions, the
deterministically-detected conflicts as a signal, and the JSON target skeleton (§5.2/§5.3).
"""
from __future__ import annotations

from typing import Any, List, Tuple

from .. import i18n

SYSTEM = (  # English source; localized via locales/<lang>.json → synthesis.system
    "You are the synthesis step of a multi-perspective panel. You are given several ROLE OPINIONS on "
    "the same question — each through its own lens.\n"
    "RULES:\n"
    "- Synthesize into ONE judgement; invent nothing that no perspective supports.\n"
    "- DO NOT SMOOTH OVER CONFLICTS: flagged contradictions must be stated EXPLICITLY (a "
    "“Conflict zones” section), never merged into a false consensus.\n"
    "- Attribute: every key claim shows WHICH role(s) carry it.\n"
    "- Emit FIRST a ```json block per the given schema, THEN concise prose.\n"
    "- Answer in the user's language; keep symbols/code in English."
)

_MODE_EXTRA = {  # English source; localized via synthesis.mode_extra[mode]
    "decision": (
        "Choose/weight criteria (1–5), score each option per criterion (1–5), give a clear "
        "recommendation AND a fallback option with a trigger. Justify it if the recommendation is not the "
        "highest weighted sum. Do NOT state your own numeric scores or weighted sums in "
        "recommendation_rationale — MPR computes and displays the weighted score itself; reference it "
        "qualitatively (which criteria drive the choice), never with invented numbers."
    ),
    "evidence-research": (
        "Assign each statement a confidence tier and back it with verbatim quotes from the opinions "
        "(role@provider). Unsupported → low confidence. List open questions."
    ),
    "comparison": (
        "Comparison matrix across the options; identify gaps and opportunities. No forced single "
        "recommendation."
    ),
}

_SKELETONS = {
    "decision-matrix": (
        '{"options":["A","B"],"criteria":[{"name":"...","weight":3,"rationale":"..."}],'
        '"cells":[{"option":"A","criterion":"...","score":4,"note":"..."}],'
        '"recommendation":"A","recommendation_rationale":"...","fallback":"B",'
        '"fallback_trigger":"...","conflict_notes":["..."]}'
    ),
    "evidence-report": (
        '{"summary":"...","findings":[{"claim":"...","confidence":"high|medium|low",'
        '"support":[{"role":"...","provider":"...","quote":"...","source_ref":"..."}],'
        '"dissent":["..."]}],"conflict_zones":["..."],"open_questions":["..."]}'
    ),
    "comparison-matrix": (
        '{"options":["A","B"],"criteria":[{"name":"...","weight":3}],'
        '"cells":[{"option":"A","criterion":"...","score":4}],'
        '"gaps":["..."],"opportunities":["..."],"conflict_notes":["..."]}'
    ),
    "risk-register": (
        '{"summary":"...","risks":[{"risk":"...","severity":"high|medium|low",'
        '"likelihood":"high|medium|low","mitigation":"...","owner":"...","roles":["..."]}],'
        '"conflict_zones":["..."],"open_questions":["..."]}'
    ),
}

#: Template-keyed guidance, added on top of the mode extra where the template diverges from its mode
#: (risk-assessment runs in evidence-research mode but emits a risk register, not an evidence report).
_TEMPLATE_EXTRA = {  # English source; localized via synthesis.template_extra[template]
    "risk-register": (
        "Build a RISK REGISTER: per risk a severity (high/medium/low) AND a likelihood (high/medium/low), "
        "a concrete mitigation and — where possible — an owner. Attribute each risk to the role(s) that "
        "raise it. NO confidence tiers."
    ),
}


def json_schema_skeleton(template: str) -> str:
    return _SKELETONS.get(template, "{}")


def build_synthesis_prompt(inp: Any, ok: List[Any], conflicts: List[Any], lang: str = "en") -> Tuple[str, str]:
    """Return (user_prompt, system_prompt) for the single synthesis call (§5), localized to *lang*
    (English source; ``locales/<lang>.json`` overlay → ``synthesis.*``)."""
    def L(key: str, default: str) -> str:
        return i18n.localized(default, lang, "synthesis", "labels", key)

    system = i18n.localized(SYSTEM, lang, "synthesis", "system")
    lines: List[str] = [
        f"{L('question', 'QUESTION')}: {inp.query}",
        f"{L('mode', 'MODE')}: {inp.mode}   {L('target_format', 'TARGET FORMAT')}: {inp.synthesis_template}",
        "",
        f"{L('gutachten', 'OPINIONS')} ({len(ok)} {L('roles', 'roles')}):",
    ]
    for i, p in enumerate(ok, 1):
        lines.append(f"[{i}] {L('role', 'Role')} «{p.role}» (provider={p.provider}, effort={p.effort}):")
        lines.append((p.content or "").strip())
        lines.append("---")
    lines += ["", L("detected_conflicts",
                    "DETECTED CONFLICTS (from the deterministic detector — treat as a signal):")]
    if conflicts:
        for c in conflicts:
            sides = " ↔ ".join(f"{', '.join(s.roles)} «{s.stance}»" for s in c.sides)
            lines.append(f"- {c.severity} [{c.kind}] {c.topic}: {sides}")
    else:
        lines.append(L("no_conflicts", "No hard conflicts detected — still check for subtle tensions."))
    # template-specific guidance overrides the mode extra where they diverge (e.g. risk-register), else
    # the mode extra carries it.
    extra_en = _TEMPLATE_EXTRA.get(inp.synthesis_template)
    if extra_en is not None:
        extra = i18n.localized(extra_en, lang, "synthesis", "template_extra", inp.synthesis_template)
    else:
        extra_en = _MODE_EXTRA.get(inp.mode, "")
        extra = i18n.localized(extra_en, lang, "synthesis", "mode_extra", inp.mode) if extra_en else ""
    if extra:
        lines += ["", extra]
    lines += ["", L("target_schema", "TARGET SCHEMA (emit ```json first):"),
              json_schema_skeleton(inp.synthesis_template)]
    return "\n".join(lines), system
