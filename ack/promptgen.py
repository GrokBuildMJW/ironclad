"""Multilingual prompt assembly (ADR-0003, #109).

Turns a `kind: prompt` item (`ack.prompt.Prompt`) + collected variable values into a finished
prompt **in a target language**. The source template is English (the SKILL.md body); a per-item
`locales/<lang>.json` overlay may translate it under the dotted key ``template`` (read via the
shared `ack.i18n.Localizer`). A missing language/overlay falls back to the source template — so a
partial or absent translation never breaks assembly. Variable placeholders are ``{name}``.

Deterministic + LLM-free (the elicitation that *collects* the values is #110). Zero external deps.
"""
from __future__ import annotations

import re
from typing import Optional

from ack.i18n import Localizer
from ack.prompt import Prompt

_PLACEHOLDER = re.compile(r"\{(\w+)\}")


class AssemblyError(Exception):
    """Required variables are missing (assembly is strict by default)."""


def missing_required(prompt: Prompt, values: dict[str, str]) -> list[str]:
    """Required variables not present (or blank) in *values* — drives the elicitation loop (#110)."""
    return [v.name for v in prompt.variables
            if v.required and not str(values.get(v.name, "")).strip()]


def localized_template(prompt: Prompt, lang: Optional[str]) -> str:
    """The template for *lang*: a `locales/<lang>.json` ``template`` overlay, else the source body."""
    if not lang or lang == "en":
        return prompt.template
    loc = Localizer(prompt.locales_dir())
    return loc.localized(prompt.template, lang, "template")


def assemble(prompt: Prompt, values: dict[str, str], *, lang: Optional[str] = None,
             strict: bool = True) -> str:
    """Render the finished prompt in *lang* (default: source). Substitutes ``{name}`` from
    *values*; declared-but-unset optional vars → empty; undeclared placeholders are left as-is.
    Raises :class:`AssemblyError` if a required variable is missing (unless ``strict=False``)."""
    if strict:
        miss = missing_required(prompt, values)
        if miss:
            raise AssemblyError(f"missing required variable(s): {', '.join(miss)}")
    declared = {v.name for v in prompt.variables}
    template = localized_template(prompt, lang)

    def _sub(m: "re.Match") -> str:
        name = m.group(1)
        if name in values:
            return str(values[name])
        if name in declared:          # declared but unset (optional) → empty
            return ""
        return m.group(0)             # undeclared placeholder → leave verbatim
    return _PLACEHOLDER.sub(_sub, template)


def _question_for(prompt: Prompt, name: str) -> str:
    v = next((x for x in prompt.variables if x.name == name), None)
    if v is None:
        return f"Provide a value for {name!r}."
    if v.question:
        return v.question
    return f"Provide a value for {name!r}" + (f" ({v.description})" if v.description else "") + "."


def run_prompt(prompt: Prompt, values: dict[str, str], *, lang: Optional[str] = None) -> dict:
    """Elicitation state machine (#110): given the values collected so far, either return the
    **next** required question, or the **assembled** prompt when all required vars are present.

    Returns ``{"status": "ask", "variable": str, "question": str, "missing": [...]}`` or
    ``{"status": "done", "prompt": str, "lang": str}``. Pure + LLM-free."""
    miss = missing_required(prompt, values)
    if miss:
        return {"status": "ask", "variable": miss[0],
                "question": _question_for(prompt, miss[0]), "missing": miss}
    return {"status": "done", "prompt": assemble(prompt, values, lang=lang),
            "lang": (lang or "en")}
