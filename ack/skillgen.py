#!/usr/bin/env python3
"""Skill generator (ADR-0001, #22/#33) — spec → a schema-valid skill scaffold, both kinds.

`spec → deterministic scaffold → (LLM fills the body) → gate → register` (ADR-0001 D3). This
module owns the **deterministic scaffold** for both skill kinds; the body is left as a clearly
marked stub for an LLM/author to fill, while the *contract* (CASE schema / SKILL.md frontmatter)
is correct **by construction**:

- **tool** — a `.py` exposing `CASE` (+ metadata) and a typed `run(...)` whose signature is the
  tool schema (discovered by `Registry.discover_skills`). For the richer paved-road domain
  scaffold (Copier-compatible, 3-way merge) use `ack.generator`; this is the single-file form.
- **playbook** — a `SKILL.md` package (frontmatter + body) + `references/` + `scripts/check`
  (the file-first validation gate), discovered by `Registry.discover_playbooks`.

Zero external dependencies (stdlib only). Outputs are secret-free and English-only.
"""
from __future__ import annotations

import argparse
import json
import keyword
import re
from dataclasses import dataclass, field
from pathlib import Path

KINDS = ("tool", "playbook")
_PARAM_TYPES = {"str": "str", "int": "int", "float": "float", "bool": "bool"}


def slugify(value: str) -> str:
    s = re.sub(r"[^\w\s-]", "", str(value).strip().lower())
    return re.sub(r"[\s_]+", "-", s).strip("-")


@dataclass
class SkillSpec:
    capability: str
    description: str
    kind: str = "tool"
    type: str = "tool"          # taxonomy: capability | artifact | tool
    domain: str = "general"
    trigger: list[str] = field(default_factory=list)
    params: list[tuple[str, str]] = field(default_factory=list)  # (name, type) for tool run()
    version: str = "0.1.0"
    provenance: str = "user"

    def __post_init__(self) -> None:
        self.capability = slugify(self.capability)
        if not self.capability:
            raise ValueError("capability is required")
        if not self.description:
            raise ValueError("description is required")
        if self.kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}, got {self.kind!r}")
        # #1536: reject anything that cannot form a valid Python ``run(...)`` signature — keep the
        # generator's "schema-valid by construction" contract. ``str.isidentifier()`` alone is not enough:
        # a reserved keyword (``class``/``def``/``None`` …) passes it yet renders `def run(class: str)` →
        # SyntaxError, and a duplicated name renders `def run(x: str, x: int)` → "duplicate argument". Both
        # would import-fail and be silently dropped at discovery. (Soft keywords like ``match``/``type`` are
        # valid parameter names, so only ``keyword.iskeyword`` — the hard keywords — is rejected.)
        seen: set[str] = set()
        for n, t in self.params:
            if not n.isidentifier():
                raise ValueError(f"invalid param name {n!r}: not a valid Python identifier")
            if keyword.iskeyword(n):
                raise ValueError(f"invalid param name {n!r}: is a reserved Python keyword")
            if t not in _PARAM_TYPES:
                raise ValueError(f"param {n!r}: type must be one of {sorted(_PARAM_TYPES)}")
            if n in seen:
                raise ValueError(f"duplicate param name {n!r}")
            seen.add(n)


def _py_module_name(cap: str) -> str:
    return re.sub(r"[^0-9a-zA-Z_]", "_", cap.replace("-", "_"))


def render_tool(spec: SkillSpec) -> dict[str, str]:
    """Render a typed CASE+run skill (+ its structural test) as {relpath: content}."""
    mod = _py_module_name(spec.capability)
    sig = ", ".join(f"{n}: {t}" for n, t in spec.params) or ""
    # GEN-1 (#503): serialize every free-text field via json.dumps so a value containing quotes,
    # backslashes or newlines yields a VALID Python/JSON string literal (raw interpolation produced a
    # SyntaxError module that would not import — and discover_skills then swallowed the error).
    case = (
        "CASE = {\n"
        f'    "name": {json.dumps(spec.capability)},\n'
        f'    "capability": {json.dumps(spec.capability)},\n'
        f'    "description": {json.dumps(spec.description)},\n'
        f'    "type": {json.dumps(spec.type)},\n'
        f'    "domain": {json.dumps(spec.domain)},\n'
        f'    "version": {json.dumps(spec.version)},\n'
        f'    "provenance": {json.dumps(spec.provenance)},\n'
        "}\n\n"
    )
    # The docstring carries the description; json.dumps(...)[1:-1] gives escaped content (quotes/backslashes/
    # newlines all escaped) that is safe inside a triple-quoted string.
    run = (
        f"def run({sig}) -> str:\n"
        f'    """{json.dumps(spec.description)[1:-1]}"""\n'
        f"    # TODO(author/LLM): implement. The CASE + signature above are the stable\n"
        f"    # contract (schema-valid by construction); fill in the body.\n"
        f'    raise NotImplementedError("skill {spec.capability!r}: body not implemented yet")\n'
    )
    test = (
        '"""Auto-generated structural test for the generated skill (scaffold gate).\n\n'
        "Checks the skill is discoverable and its tool schema is well-formed. Behavioral\n"
        "assertions are added once the run() body is filled.\n"
        '"""\n'
        "from pathlib import Path\n\n"
        "from ack.registry import Registry, derive_tool_schema\n\n\n"
        f"def test_{mod}_is_discoverable_with_a_schema(tmp_path=None):\n"
        f"    root = Path(__file__).resolve().parents[1]\n"
        f"    regs = {{r.capability: r for r in Registry().discover_skills(str(root))}}\n"
        f'    assert "{spec.capability}" in regs\n'
        f'    schema = derive_tool_schema(regs["{spec.capability}"].handler)\n'
        f'    assert schema["type"] == "object"\n'
    )
    return {
        f"skills/{mod}.py": case + run,
        f"tests/test_{mod}.py": test,
    }


def render_playbook(spec: SkillSpec) -> dict[str, str]:
    """Render a SKILL.md playbook package as {relpath: content}."""
    trig = "[" + ", ".join(spec.trigger) + "]" if spec.trigger else "[]"
    # GEN-1 (#503): the frontmatter parser is a naive flat `key: scalar` reader (it splits on the first
    # ':' and keeps the rest verbatim), so a colon/quote/backslash in a value round-trips fine — the only
    # break is a NEWLINE, which would spill into a bogus frontmatter line. Flatten free-text values to a
    # single line. (The full multi-line description is preserved in the body below.)
    _flat = lambda s: " ".join(str(s).split())
    skill_md = (
        "---\n"
        f"capability: {spec.capability}\n"
        f"name: {spec.capability}\n"
        f"description: {_flat(spec.description)}\n"
        "kind: playbook\n"
        f"type: {_flat(spec.type)}\n"
        f"domain: {_flat(spec.domain)}\n"
        f"trigger: {trig}\n"
        f'version: "{spec.version}"\n'
        f"provenance: {_flat(spec.provenance)}\n"
        "---\n\n"
        f"# {spec.capability}\n\n"
        f"{spec.description}\n\n"
        "## When to use\n\n"
        "<!-- TODO(author/LLM): the trigger conditions + routing. -->\n\n"
        "## Steps\n\n"
        "<!-- TODO(author/LLM): the instructions. Load a reference with "
        "`use_skill('" + spec.capability + "', '<name>')` only when needed (progressive disclosure). -->\n"
    )
    check = (
        "#!/usr/bin/env python3\n"
        '"""File-first validation gate for this playbook (run by the registration gate, #34).\n\n'
        "Self-contained (no imports) so it runs from any layout: checks the SKILL.md has a\n"
        "closed frontmatter block carrying the required fields.\n"
        '"""\n'
        "import sys\n"
        "from pathlib import Path\n\n"
        "md = (Path(__file__).resolve().parents[1] / 'SKILL.md').read_text(encoding='utf-8')\n"
        "lines = md.splitlines()\n"
        "if not lines or lines[0].strip() != '---' or '---' not in [l.strip() for l in lines[1:]]:\n"
        "    print('INVALID: missing/!unclosed frontmatter'); sys.exit(1)\n"
        "fm = '\\n'.join(lines[1:1 + [l.strip() for l in lines[1:]].index('---')])\n"
        "missing = [k for k in ('capability', 'description', 'kind') if f'{k}:' not in fm]\n"
        "if missing:\n"
        "    print(f'INVALID: missing fields {missing}'); sys.exit(1)\n"
        "print('OK'); sys.exit(0)\n"
    )
    return {
        f"skills/{spec.capability}/SKILL.md": skill_md,
        f"skills/{spec.capability}/references/.gitkeep": "",
        f"skills/{spec.capability}/scripts/check": check,
    }


def scaffold(spec: SkillSpec) -> dict[str, str]:
    """Render the scaffold for *spec* as {relpath: content} (kind-dispatched)."""
    return render_tool(spec) if spec.kind == "tool" else render_playbook(spec)


def write_scaffold(spec: SkillSpec, dest: str | Path, *, force: bool = False) -> list[Path]:
    """Write the scaffold under *dest*. Refuses to overwrite unless *force*. Returns paths."""
    dest = Path(dest)
    items = list(scaffold(spec).items())
    # #1537: preflight the COMPLETE target set before writing anything. Writing/refusing one target at a
    # time meant a conflict on a LATER target (e.g. an existing sibling test) raised only AFTER an earlier
    # target was already committed, leaving a half-scaffolded destination — a discoverable skill without its
    # generated test — despite the "refuses to overwrite" contract. Now a single conflict refuses atomically.
    if not force:
        for rel, _ in items:
            target = dest / rel
            if target.exists():
                raise FileExistsError(f"refusing to overwrite {target} (use force=True)")
    written: list[Path] = []
    for rel, content in items:
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        written.append(target)
    return written


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Scaffold a skill (tool or playbook) from a spec.")
    ap.add_argument("--capability", required=True)
    ap.add_argument("--description", required=True)
    ap.add_argument("--kind", choices=KINDS, default="tool")
    ap.add_argument("--type", default="tool")
    ap.add_argument("--domain", default="general")
    ap.add_argument("--trigger", default="", help="comma-separated trigger phrases (playbook)")
    ap.add_argument("--param", action="append", default=[], metavar="name:type",
                    help="tool run() parameter, e.g. --param text:str (repeatable)")
    ap.add_argument("--dest", default=".")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)
    params = []
    for p in args.param:
        n, _, t = p.partition(":")
        params.append((n.strip(), (t.strip() or "str")))
    spec = SkillSpec(
        capability=args.capability, description=args.description, kind=args.kind,
        type=args.type, domain=args.domain,
        trigger=[t.strip() for t in args.trigger.split(",") if t.strip()],
        params=params,
    )
    written = write_scaffold(spec, args.dest, force=args.force)
    for p in written:
        print(f"wrote {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
