"""Registration quality gate (ADR-0001 D2, #34) — no unchecked skill enters the toolset.

Before a generated/installed skill is registered it must pass this gate:

- **tool** — a doctor preflight: the module parses + loads, exposes a ``CASE`` with a
  non-empty ``capability``, a **synchronous** ``run`` whose signature yields a valid tool
  schema; and an auto-generated **test file** ships alongside it.
- **playbook** — its ``SKILL.md`` frontmatter validates against the schema, its references are
  readable, and its ``scripts/check`` (the file-first gate) exits 0 if present.

The heavier behavioral ``eval/`` (A/B + judge) stays **opt-in** (not part of this gate).
Pure/deterministic except the optional ``scripts/check`` subprocess.
"""
from __future__ import annotations

import inspect
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class GateResult:
    passed: bool
    kind: str
    reasons: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.passed


def gate_tool(py_path: str | Path) -> GateResult:
    """Doctor preflight for a typed ``CASE``+``run`` skill file."""
    from ack.doctor import load_module_by_path, syntax_error
    from ack.registry import derive_tool_schema

    p = Path(py_path)
    reasons: list[str] = []
    if not p.is_file():
        return GateResult(False, "tool", [f"no such file: {p}"])
    se = syntax_error(p)
    if se:
        return GateResult(False, "tool", [f"syntax error: {se}"])
    try:
        mod = load_module_by_path(f"_gate_{p.stem}", p)
    except Exception as e:  # noqa: BLE001 — a load failure is a gate failure, surfaced
        return GateResult(False, "tool", [f"import failed: {e!r}"])

    case = getattr(mod, "CASE", None)
    if not isinstance(case, dict):
        reasons.append("no CASE dict")
    elif not str(case.get("capability") or "").strip():
        reasons.append("CASE has no non-empty 'capability'")
    run = getattr(mod, "run", None)
    if not callable(run):
        reasons.append("no callable run()")
    else:
        if inspect.iscoroutinefunction(run):
            reasons.append("run() must be synchronous (async not allowed on the tool path)")
        try:
            schema = derive_tool_schema(run)
            if not isinstance(schema, dict) or schema.get("type") != "object":
                reasons.append("run() does not yield a valid object tool schema")
        except Exception as e:  # noqa: BLE001
            reasons.append(f"tool schema not derivable: {e!r}")

    # "ships with auto-generated tests": a sibling tests/test_<stem>.py must exist
    test_file = p.parent.parent / "tests" / f"test_{p.stem}.py"
    if not test_file.is_file():
        reasons.append(f"no auto-generated test ({test_file.name}) — unchecked code")

    return GateResult(not reasons, "tool", reasons)


def gate_playbook(skill_md: str | Path, *, run_check: bool = True) -> GateResult:
    """Validate a playbook package: frontmatter schema + readable references + scripts/check."""
    from ack.playbook import PlaybookError, parse_playbook, validate_meta

    p = Path(skill_md)
    if p.is_dir():
        p = p / "SKILL.md"
    if not p.is_file():
        return GateResult(False, "playbook", [f"no SKILL.md at {p}"])
    reasons: list[str] = []
    try:
        pb = parse_playbook(p)
        reasons.extend(validate_meta(pb.meta))
    except PlaybookError as e:
        return GateResult(False, "playbook", [f"frontmatter invalid: {e}"])

    refs_dir = p.parent / "references"
    if refs_dir.is_dir():
        for ref in refs_dir.glob("*"):
            if ref.is_file() and ref.name != ".gitkeep":
                try:
                    ref.read_text(encoding="utf-8")
                except OSError as e:
                    reasons.append(f"reference {ref.name!r} unreadable: {e}")

    check = p.parent / "scripts" / "check"
    if run_check and check.is_file():
        try:
            cp = subprocess.run([sys.executable, str(check)], capture_output=True,
                                text=True, timeout=60)
            if cp.returncode != 0:
                reasons.append(f"scripts/check failed (rc={cp.returncode}): "
                               f"{(cp.stdout + cp.stderr).strip()[:200]}")
        except (OSError, subprocess.SubprocessError) as e:
            reasons.append(f"scripts/check not runnable: {e!r}")

    return GateResult(not reasons, "playbook", reasons)


def gate_prompt(skill_md: str | Path) -> GateResult:
    """Eval/registration gate for a ``kind: prompt`` item (#111).

    A prompt passes iff: its frontmatter validates (``ack.prompt`` schema), every **required**
    variable actually appears as a ``{placeholder}`` in the template (a required input that can't
    affect the output is a defect), and it **assembles cleanly in every declared language** —
    proving the `locales/<lang>.json` overlays are readable and well-formed (a missing overlay is
    fine: it falls back to source). Deterministic, model-free.
    """
    from ack.prompt import PromptError, parse_prompt
    from ack.promptgen import _PLACEHOLDER, assemble

    p = Path(skill_md)
    if p.is_dir():
        p = p / "SKILL.md"
    if not p.is_file():
        return GateResult(False, "prompt", [f"no SKILL.md at {p}"])
    try:
        prompt = parse_prompt(p)
    except PromptError as e:
        return GateResult(False, "prompt", [f"frontmatter invalid: {e}"])

    import json

    reasons: list[str] = []
    placeholders = set(_PLACEHOLDER.findall(prompt.template))
    for v in prompt.variables:
        if v.required and v.name not in placeholders:
            reasons.append(f"required variable {v.name!r} is never used in the template")

    sample = {v.name: f"<{v.name}>" for v in prompt.variables}
    for lang in prompt.languages:
        # A *missing* overlay is fine (intentional English fallback); a *present* one that is
        # malformed is a defect — the runtime would silently fall back, masking a broken
        # translation. The gate is where "assemblable in DE+EN" must actually mean DE works.
        if lang != "en":
            overlay = prompt.locales_dir() / f"{lang}.json"
            if overlay.is_file():
                try:
                    data = json.loads(overlay.read_text(encoding="utf-8"))
                    if not isinstance(data, dict):
                        reasons.append(f"{lang!r} overlay {overlay.name} is not a JSON object")
                    elif not str(data.get("template", "")).strip():
                        reasons.append(f"{lang!r} overlay {overlay.name} has no 'template' string")
                except (OSError, ValueError) as e:
                    reasons.append(f"{lang!r} overlay {overlay.name} is unreadable/invalid JSON: {e}")
        try:
            assemble(prompt, sample, lang=lang)   # all vars provided → strict is fine
        except Exception as e:  # noqa: BLE001 — any assembly failure is a gate failure, surfaced
            reasons.append(f"not assemblable in {lang!r}: {e!r}")

    return GateResult(not reasons, "prompt", reasons)


def gate(path: str | Path, **kw) -> GateResult:
    """Dispatch to the right gate by item kind/path shape: ``kind: prompt`` SKILL.md → prompt;
    other SKILL.md/dir → playbook; ``.py`` → tool."""
    from ack.prompt import is_prompt_item

    p = Path(path)
    if p.is_dir() or p.name == "SKILL.md":
        md = p / "SKILL.md" if p.is_dir() else p
        if md.is_file() and is_prompt_item(md):
            return gate_prompt(md)
        return gate_playbook(p, **kw)
    return gate_tool(p)
