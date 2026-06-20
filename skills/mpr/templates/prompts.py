"""Synthesis prompt contract (Spec 06 §5) — one shared system prompt + a per-mode user prompt.

LLM-free prompt *construction* only (the call itself is the injected ``llm_call`` in synthesis.py). The
system prompt forbids smoothing conflicts away (§5.1); the user prompt lays out the role gutachten, the
deterministically-detected conflicts as a signal, and the JSON target skeleton (§5.2/§5.3).
"""
from __future__ import annotations

from typing import Any, List, Tuple

SYSTEM = (
    "Du bist der Synthese-Schritt eines Multi-Perspektiven-Panels. Dir liegen mehrere "
    "ROLLEN-GUTACHTEN zur selben Frage vor — jedes aus einer eigenen Brille.\n"
    "REGELN:\n"
    "- Synthetisiere zu EINEM Urteil; erfinde nichts, was keine Perspektive belegt.\n"
    "- KONFLIKTE NICHT GLÄTTEN: markierte Widersprüche musst du EXPLIZIT ausweisen "
    "(Abschnitt „Konfliktzonen“), niemals zu einem falschen Konsens verschmelzen.\n"
    "- Attribuiere: jede Kernaussage zeigt, WELCHE Rolle(n) sie trägt.\n"
    "- Emit ZUERST einen ```json-Block nach dem vorgegebenen Schema, DANN knappe Prosa.\n"
    "- Antworte auf Deutsch; Symbole/Code englisch."
)

_MODE_EXTRA = {
    "decision": (
        "Wähle/gewichte Kriterien (1–5), bewerte jede Option je Kriterium (1–5), gib eine klare "
        "Empfehlung UND eine Rückzugsoption mit Auslöser. Begründe, falls die Empfehlung nicht der "
        "höchsten gewichteten Summe entspricht."
    ),
    "evidence-research": (
        "Ordne jede Aussage einem Confidence-Tier zu und belege sie mit wörtlichen Zitaten aus den "
        "Gutachten (Rolle@Provider). Unbelegtes → niedrige Konfidenz. Liste offene Fragen."
    ),
    "comparison": (
        "Vergleichs-Matrix über die Optionen; identifiziere Lücken und Chancen. Keine erzwungene "
        "Einzel-Empfehlung."
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
_TEMPLATE_EXTRA = {
    "risk-register": (
        "Erstelle ein RISIKO-REGISTER: je Risiko Schwere (high/medium/low) UND Eintritts-"
        "wahrscheinlichkeit (high/medium/low), eine konkrete Mitigation und — wo möglich — einen Owner. "
        "Attribuiere jedes Risiko der/den Rolle(n), die es aufwerfen. KEINE Confidence-Tiers."
    ),
}


def json_schema_skeleton(template: str) -> str:
    return _SKELETONS.get(template, "{}")


def build_synthesis_prompt(inp: Any, ok: List[Any], conflicts: List[Any]) -> Tuple[str, str]:
    """Return (user_prompt, system_prompt) for the single synthesis call (§5)."""
    lines: List[str] = [
        f"FRAGE: {inp.query}",
        f"MODUS: {inp.mode}   ZIEL-FORMAT: {inp.synthesis_template}",
        "",
        f"GUTACHTEN ({len(ok)} Rollen):",
    ]
    for i, p in enumerate(ok, 1):
        lines.append(f"[{i}] Rolle «{p.role}» (provider={p.provider}, effort={p.effort}):")
        lines.append((p.content or "").strip())
        lines.append("---")
    lines += ["", "ERKANNTE KONFLIKTE (vom deterministischen Detektor — als Signal behandeln):"]
    if conflicts:
        for c in conflicts:
            sides = " ↔ ".join(f"{', '.join(s.roles)} «{s.stance}»" for s in c.sides)
            lines.append(f"- {c.severity} [{c.kind}] {c.topic}: {sides}")
    else:
        lines.append("Keine harten Konflikte erkannt — prüfe dennoch auf subtile Spannungen.")
    # template-specific guidance overrides the mode extra where they diverge (e.g. risk-register), else
    # the mode extra carries it.
    extra = _TEMPLATE_EXTRA.get(inp.synthesis_template) or _MODE_EXTRA.get(inp.mode)
    if extra:
        lines += ["", extra]
    lines += ["", "ZIEL-SCHEMA (```json zuerst ausgeben):", json_schema_skeleton(inp.synthesis_template)]
    return "\n".join(lines), SYSTEM
