"""Prompt-library item (`kind: prompt`) — ADR-0003, #108.

A prompt is a declarative MD item: a `SKILL.md` with a `kind: prompt` frontmatter (variables +
languages + per-variable elicitation) and a **template** body. It reuses the shared
`ack.playbook.parse_frontmatter` (one parser — no parallel infra) and is discovered as a core
built-in. Distinct from `kind: playbook` (instructions the model reads): a prompt is a template
the user fills (via elicitation) to *produce* a finished prompt — assembled deterministically
(see `ack.promptgen`, #109) and offered as a slash-command (#110).

Frontmatter encoding (flat, so the existing parser handles it — no YAML-block surgery):

    ---
    capability: blog-post
    kind: prompt
    description: Draft a blog post brief
    type: prompt
    domain: writing
    languages: [en, de]
    variables: [topic, audience]      # the inputs to elicit
    required: [topic]                 # subset that must be provided (others optional)
    ask.topic: What is the topic?     # per-variable elicitation question (optional)
    ask.audience: Who is the audience?
    desc.topic: The subject of the post   # per-variable description (optional)
    version: "0.1.0"
    provenance: built-in
    ---
    Write a {audience}-facing post about {topic}.

Zero external dependencies (stdlib only).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ack.playbook import parse_frontmatter  # one shared frontmatter parser

KIND = "prompt"
_REQUIRED = ("capability", "kind", "description")


class PromptError(Exception):
    """A prompt item could not be parsed or failed schema validation."""


@dataclass
class Variable:
    name: str
    required: bool = True
    description: str = ""
    question: str = ""          # elicitation prompt shown to the user


def validate_prompt_meta(meta: dict[str, Any]) -> list[str]:
    """Return schema violations ([] = valid). Mirrors docs/prompt-packaging.md."""
    errs: list[str] = []
    for r in _REQUIRED:
        if not meta.get(r):
            errs.append(f"missing required field: {r}")
    if meta.get("kind") not in (None, KIND):
        errs.append(f"kind must be {KIND!r}, got {meta.get('kind')!r}")
    if "variables" in meta and not isinstance(meta["variables"], list):
        errs.append("field 'variables' must be a list")
    if "required" in meta and not isinstance(meta["required"], list):
        errs.append("field 'required' must be a list")
    if "languages" in meta and not isinstance(meta["languages"], list):
        errs.append("field 'languages' must be a list")
    return errs


def _build_variables(meta: dict[str, Any]) -> list[Variable]:
    names = [str(n) for n in meta.get("variables", []) if str(n).strip()]
    required = {str(n) for n in meta.get("required", [])}
    out: list[Variable] = []
    for n in names:
        out.append(Variable(
            name=n,
            required=(n in required) if required or "required" in meta else True,
            description=str(meta.get(f"desc.{n}", "") or ""),
            question=str(meta.get(f"ask.{n}", "") or ""),
        ))
    return out


class Prompt:
    """A discovered prompt item. ``meta``/``variables`` eager; ``template``/locales lazy."""

    def __init__(self, skill_md: Path, meta: dict[str, Any], template: str) -> None:
        self.path = skill_md.resolve()
        self.dir = self.path.parent
        self.meta = meta
        self.variables: list[Variable] = _build_variables(meta)
        self._template = template

    @property
    def capability(self) -> str:
        return str(self.meta["capability"])

    @property
    def description(self) -> str:
        return str(self.meta.get("description") or "")

    @property
    def languages(self) -> list[str]:
        langs = [str(x) for x in self.meta.get("languages", []) if str(x).strip()]
        return langs or ["en"]

    @property
    def template(self) -> str:
        return self._template

    def locales_dir(self) -> Path:
        return self.dir / "locales"

    def metadata(self) -> dict[str, Any]:
        return {
            "capability": self.capability, "kind": KIND, "description": self.description,
            "type": self.meta.get("type"), "domain": self.meta.get("domain"),
            "languages": self.languages, "version": self.meta.get("version"),
            "provenance": self.meta.get("provenance"),
            "variables": [v.name for v in self.variables],
        }


def parse_prompt(skill_md: Path) -> Prompt:
    """Parse + validate one `kind: prompt` SKILL.md. Raises PromptError on a bad item."""
    skill_md = Path(skill_md)
    meta, body = parse_frontmatter(skill_md.read_text(encoding="utf-8"))
    meta.setdefault("kind", KIND)
    errs = validate_prompt_meta(meta)
    if errs:
        raise PromptError(f"{skill_md}: invalid prompt frontmatter — {'; '.join(errs)}")
    return Prompt(skill_md, meta, body)


def is_prompt_item(skill_md: Path) -> bool:
    """True iff *skill_md*'s frontmatter declares ``kind: prompt`` (cheap, fail-soft)."""
    try:
        meta, _ = parse_frontmatter(Path(skill_md).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return False
    return meta.get("kind") == KIND


def discover_prompts(root: str | Path) -> list[Prompt]:
    """Walk *root* for `kind: prompt` SKILL.md items (fail-soft; dedup by capability)."""
    base = Path(root)
    if not base.is_dir():
        return []
    out: list[Prompt] = []
    seen: set[str] = set()
    import logging
    log = logging.getLogger(__name__)
    for skill_md in sorted(base.glob("**/SKILL.md")):
        if not is_prompt_item(skill_md):
            continue
        try:
            p = parse_prompt(skill_md)
        except (PromptError, OSError) as exc:
            log.warning("prompt: skipping unloadable %s: %s", skill_md, exc)
            continue
        if p.capability in seen:
            log.warning("prompt: duplicate capability %r (%s) — keeping first", p.capability, skill_md)
            continue
        seen.add(p.capability)
        out.append(p)
    return out
