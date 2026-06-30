#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ACK "Paved Road" Generator — scaffolds a new Case/Domain from a template tree.

Renders the complete skeleton for a new case from the template under
``<generator>/templates/new-case/`` — Case-Spec + gap-tracking/backlog (with the
EXACT markers/frontmatter the Lodestar capability tracker expects) + a procedural
Skill stub + Tests + a self-discovering registration stub + a README with
backlinks. One command instead of the hand-ritual that used to take many
iterations before a backlog stood.

WHY stdlib (no Copier dependency)
---------------------------------
Copier is not required: this CLI does the rendering + re-runnable 3-way merge
itself, in ~0 dependencies, while the template tree is authored Copier-compatible
(a later ``pip install copier`` renders the same ``copier.yml`` + ``{{ ... }}``
tree identically). This script is the stdlib fallback / reference implementation.

RENDERING (substitution-only)
-----------------------------
Only ``{{ token }}`` placeholders are substituted (with or without inner spaces),
in BOTH path components and file contents. No ``str.format``, no Jinja, no
attribute/index access is expressible. Unknown tokens are left verbatim
(fail-soft) and reported.

RE-RUNNABLE (3-way merge)
-------------------------
Per generated file we remember the exact bytes we last rendered (the "base") in
``<domain>/.ack-generator-state.json``. On re-run a line-based diff3 merges base
(last template baseline) / mine (on-disk, may carry edits) / theirs (freshly
rendered template): a template-only change upgrades, a local-only edit is
preserved, identical changes collapse, genuine divergence is wrapped in conflict
markers and the run reports non-zero. New files are created; a pre-existing
untracked file is skipped unless ``--force``. Identical re-run = idempotent no-op.

USAGE
-----
    python -m ack.generator --domain my-domain \\
        --case my-feature --description "What this case does" \\
        [--kind case|prompt] \\
        [--prefix x] [--phase MVP] [--tier high] [--type implementation] \\
        [--assignee claude-opus-4-8] [--effort high] [--tags "tag1,tag2"] \\
        [--output-root cases] [--template <dir>] [--force] [--dry-run] \\
        [--reserved-capabilities cap1,cap2]

``--kind case`` (default) renders the ``new-case`` paved road (a CASE+run tool +
spec/backlog/gap-tracking/tests). ``--kind prompt`` renders the ``new-prompt`` tree
(a ``kind: prompt`` library item: ``SKILL.md`` + ``locales/<lang>.json``), which is
gate-valid (``ack.gate.gate_prompt``) on first render and ready to customise.

Exit codes: 0 = clean; 2 = merge conflict(s) written (resolve by hand) OR the run was REFUSED by the
built-in collision guard (`--reserved-capabilities`); 1 = error.

NOTE: the template tree (``templates/new-case/``) is ported separately — see the
demo vessel. The generator code here is generic and template-tree agnostic.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path

# Templates ship bundled next to the generator (package-relative); output lands
# under a generic, cwd-relative "cases/" dir by default (override via --output-root).
ROOT = Path(__file__).resolve().parent
DEFAULT_TEMPLATE = ROOT / "templates" / "new-case"
PROMPT_TEMPLATE = ROOT / "templates" / "new-prompt"
#: Built-in template tree per ``--kind`` (the engine picks the right one; ``--template`` overrides).
TEMPLATE_BY_KIND = {"case": DEFAULT_TEMPLATE, "prompt": PROMPT_TEMPLATE}
DEFAULT_OUTPUT_ROOT = Path("cases")
STATE_FILENAME = ".ack-generator-state.json"

# Files at the template root that are NOT part of the rendered output.
TEMPLATE_SKIP = {"copier.yml", "copier.yaml", "TEMPLATE-README.md"}

_TOKEN_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


# --------------------------------------------------------------------------- #
# Derivations (mirror copier.yml computed defaults)                           #
# --------------------------------------------------------------------------- #
def slugify(value: str) -> str:
    """Kebab-case slug: lowercase, non-alnum runs -> single hyphen, trimmed."""
    s = re.sub(r"[^a-z0-9]+", "-", value.strip().lower())
    return s.strip("-")


def title_folder(domain_name: str) -> str:
    """agent-contract-kernel -> Agent-Contract-Kernel (Research subfolder name)."""
    return "-".join(part.capitalize() for part in slugify(domain_name).split("-") if part)


def humanize(domain_name: str) -> str:
    """agent-contract-kernel -> Agent Contract Kernel (human title)."""
    return " ".join(part.capitalize() for part in slugify(domain_name).split("-") if part)


def initials(domain_name: str) -> str:
    """agent-contract-kernel -> ack (default short capability prefix)."""
    parts = [p for p in slugify(domain_name).split("-") if p]
    return "".join(p[0] for p in parts) or slugify(domain_name)


def build_context(args: argparse.Namespace) -> dict[str, str]:
    domain_name = slugify(args.domain)
    case_name = slugify(args.case)
    key_prefix = slugify(args.prefix) if args.prefix else initials(domain_name)
    tags = args.tags if args.tags is not None else f"tracking, {domain_name}, planning, gx10"
    tags_list = [t.strip() for t in tags.split(",") if t.strip()]
    ctx = {
        "domain_name": domain_name,
        "domain_folder": title_folder(domain_name),
        "domain_title": humanize(domain_name),
        "case_name": case_name,
        "case_title": humanize(case_name),
        "key_prefix": key_prefix,
        "capability_key": f"{key_prefix}-{case_name}",
        "description": args.description,
        "phase": args.phase,
        "tier": args.tier,
        "type": args.type,
        "assignee": args.assignee,
        "effort": args.effort,
        "non_negotiable": "true" if args.non_negotiable else "false",
        "tags_csv": ", ".join(tags_list),
        "tags_yaml": "[" + ", ".join(tags_list) + "]",
        "date": str(date.today()),
    }
    return ctx


# --------------------------------------------------------------------------- #
# Substitution-only renderer                                #
# --------------------------------------------------------------------------- #
def template_root_for(args: argparse.Namespace) -> Path:
    """Resolve the template tree: an explicit ``--template`` wins; otherwise pick the built-in tree for
    ``--kind`` (``case`` → ``new-case`` paved road [default, byte-identical], ``prompt`` → ``new-prompt``
    prompt-library scaffold). Keeping the default in one place lets both the CLI and the engine agree."""
    explicit = getattr(args, "template", None)
    if explicit:
        return Path(explicit)
    return TEMPLATE_BY_KIND.get(getattr(args, "kind", "case"), DEFAULT_TEMPLATE)


def render_str(text: str, ctx: dict[str, str], unknown: set[str] | None = None) -> str:
    def repl(m: re.Match) -> str:
        key = m.group(1)
        if key in ctx:
            return str(ctx[key])
        if unknown is not None:
            unknown.add(key)
        return m.group(0)  # leave verbatim (fail-soft)

    return _TOKEN_RE.sub(repl, text)


def render_path(rel: Path, ctx: dict[str, str], unknown: set[str] | None = None) -> Path:
    return Path(*[render_str(part, ctx, unknown) for part in rel.parts])


# --------------------------------------------------------------------------- #
# 3-way merge (diff3, line-based)                                             #
# --------------------------------------------------------------------------- #
def _equal_map(base: list[str], other: list[str]) -> dict[int, int]:
    """base-index -> other-index for every line equal under the LCS alignment."""
    eq: dict[int, int] = {}
    for i1, j1, n in SequenceMatcher(None, base, other, autojunk=False).get_matching_blocks():
        for k in range(n):
            eq[i1 + k] = j1 + k
    return eq


def three_way_merge(
    base: str,
    mine: str,
    theirs: str,
    *,
    local_label: str = "local",
    template_label: str = "template",
) -> tuple[str, bool]:
    """diff3 merge. Returns (text, conflicted).

    base   = last generator-rendered content
    mine   = current on-disk content (local edits)
    theirs = freshly rendered template (template upgrade)
    """
    if mine == theirs:
        return mine, False
    if mine == base:
        return theirs, False  # only the template moved
    if theirs == base:
        return mine, False  # only the local edit moved

    b = base.splitlines(keepends=True)
    a = mine.splitlines(keepends=True)
    t = theirs.splitlines(keepends=True)
    em = _equal_map(b, a)
    et = _equal_map(b, t)
    sync = [i for i in range(len(b)) if i in em and i in et]

    out: list[str] = []
    conflicted = False
    bi = ai = ti = 0
    for s in sync + [None]:
        if s is None:
            b_end, a_end, t_end = len(b), len(a), len(t)
        else:
            b_end, a_end, t_end = s, em[s], et[s]
        o_chunk = b[bi:b_end]
        a_chunk = a[ai:a_end]
        t_chunk = t[ti:t_end]
        if a_chunk == o_chunk and t_chunk == o_chunk:
            out.extend(o_chunk)
        elif a_chunk == o_chunk:
            out.extend(t_chunk)  # only template changed -> upgrade
        elif t_chunk == o_chunk:
            out.extend(a_chunk)  # only local changed -> preserve
        elif a_chunk == t_chunk:
            out.extend(a_chunk)  # both made the same change
        else:
            conflicted = True
            out.append(f"<<<<<<< {local_label}\n")
            out.extend(a_chunk)
            if out and not out[-1].endswith("\n"):
                out.append("\n")
            out.append("=======\n")
            out.extend(t_chunk)
            if out and not out[-1].endswith("\n"):
                out.append("\n")
            out.append(f">>>>>>> {template_label}\n")
        if s is not None:
            out.append(b[s])
            bi, ai, ti = s + 1, em[s] + 1, et[s] + 1
    return "".join(out), conflicted


# --------------------------------------------------------------------------- #
# Generation                                                                  #
# --------------------------------------------------------------------------- #
@dataclass
class FileResult:
    rel: str
    action: str  # created | upgraded | unchanged | conflict | skipped
    detail: str = ""


@dataclass
class GenerateResult:
    files: list[FileResult] = field(default_factory=list)
    unknown_tokens: set[str] = field(default_factory=set)
    conflicts: int = 0
    domain_dir: Path | None = None
    refused: str = ""               # #601 S10: non-empty => the run was refused (built-in collision); nothing written

    @property
    def ok(self) -> bool:
        return self.conflicts == 0 and not self.refused


def iter_template_files(template_root: Path):
    for p in sorted(template_root.rglob("*")):
        if p.is_dir():
            continue
        rel = p.relative_to(template_root)
        if len(rel.parts) == 1 and rel.parts[0] in TEMPLATE_SKIP:
            continue
        yield rel, p


def _load_state(state_path: Path) -> dict:
    if state_path.exists():
        try:
            return json.loads(state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"version": 1, "answers": {}, "files": {}}


def generate(
    ctx: dict[str, str],
    *,
    template_root: Path = DEFAULT_TEMPLATE,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    force: bool = False,
    dry_run: bool = False,
    reserved_capabilities: "set[str] | None" = None,
) -> GenerateResult:
    if not template_root.is_dir():
        raise FileNotFoundError(f"Template root not found: {template_root}")

    result = GenerateResult()
    domain_dir = output_root / ctx["domain_folder"]
    result.domain_dir = domain_dir

    # #601 S10 (ADR-0011): built-in collision guard. The generator writes into a PER-PROJECT library; a
    # generated capability that shadows a core built-in (e.g. `mpr`) would be ambiguous at load time, so it
    # is REFUSED fail-closed before anything is written — never silently overwrite/shadow a built-in. The
    # reserved set is injected by the engine (the core/skills built-in capabilities); empty/None => no guard
    # (byte-identical to the pre-guard generator).
    cap = ctx.get("capability_key", "")
    if reserved_capabilities and cap in reserved_capabilities:
        result.refused = (f"capability {cap!r} collides with a built-in — refused (a per-project item may "
                          f"not shadow a core built-in); choose a different --prefix/--case")
        return result
    state_path = domain_dir / STATE_FILENAME
    state = _load_state(state_path)
    base_files: dict[str, str] = state.get("files", {})
    new_base: dict[str, str] = dict(base_files)

    for rel, src in iter_template_files(template_root):
        out_rel = render_path(rel, ctx, result.unknown_tokens)
        rel_key = out_rel.as_posix()
        raw = src.read_text(encoding="utf-8")
        rendered = render_str(raw, ctx, result.unknown_tokens)
        target = output_root / out_rel

        if not target.exists():
            action, content, detail = "created", rendered, ""
        else:
            current = target.read_text(encoding="utf-8")
            base = base_files.get(rel_key)
            if base is None:
                if force:
                    merged, conflicted = three_way_merge(current, current, rendered)
                    action = "conflict" if conflicted else ("upgraded" if merged != current else "unchanged")
                    content, detail = merged, "untracked (--force)"
                else:
                    # GEN-2 (#503): do NOT record a baseline for a SKIPPED untracked file — recording
                    # `rendered` as the base made the NEXT run three-way-merge the user's declined file
                    # against a phantom base, producing spurious diff3 conflicts. No baseline ⇒ the file
                    # stays untracked and is skipped again (idempotent for a declined file).
                    result.files.append(FileResult(rel_key, "skipped", "exists, untracked (use --force)"))
                    continue
            else:
                merged, conflicted = three_way_merge(base, current, rendered)
                content = merged
                if conflicted:
                    action, detail = "conflict", "diff3 markers written"
                elif merged == current:
                    action, detail = "unchanged", ""
                else:
                    action, detail = "upgraded", "3-way merged"

        if action == "conflict":
            result.conflicts += 1
        new_base[rel_key] = rendered  # template baseline tracks the latest render
        if not dry_run and action in ("created", "upgraded", "conflict"):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        result.files.append(FileResult(rel_key, action, detail))

    if not dry_run:
        domain_dir.mkdir(parents=True, exist_ok=True)
        state["answers"] = ctx
        state["files"] = new_base
        state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    return result


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="generate-case.py",
        description="ACK paved-road generator: scaffold a new Case/Domain (re-runnable, 3-way merge).",
    )
    p.add_argument("--domain", required=True, help="Domain key (kebab), e.g. agent-contract-kernel")
    p.add_argument("--case", required=True, help="Case key (kebab), e.g. my-feature")
    p.add_argument("--description", required=True, help="One-line description of the case")
    p.add_argument("--kind", default="case", choices=["case", "prompt"],
                   help="What to scaffold: 'case' (a CASE+run tool, default) or 'prompt' "
                        "(a kind: prompt library item). Selects the built-in template tree.")
    p.add_argument("--prefix", default=None, help="Capability key prefix (default: domain initials, e.g. 'ack')")
    p.add_argument("--phase", default="MVP", choices=["MVP", "V1", "V2", "V3", "out-of-scope"])
    p.add_argument("--tier", default="high", choices=["high", "medium", "low"])
    p.add_argument("--type", default="implementation",
                   help="Task type (implementation/architecture/documentation/security…)")
    p.add_argument("--assignee", default="claude-opus-4-8")
    p.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh"])
    p.add_argument("--non-negotiable", action="store_true", help="Mark the seed feature as Non-Negotiable")
    p.add_argument("--tags", default=None, help="Comma-separated frontmatter tags (default derived)")
    p.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT),
                   help="Where the domain folder is written (default: cases)")
    p.add_argument("--template", default=None,
                   help="Template root directory (default: the built-in tree for --kind)")
    p.add_argument("--force", action="store_true", help="Merge into pre-existing untracked files")
    p.add_argument("--dry-run", action="store_true", help="Report actions without writing")
    p.add_argument("--reserved-capabilities", default=None,
                   help="Comma-separated built-in capabilities a generated item may NOT shadow "
                        "(the engine injects the core/skills built-ins; collision => refused)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ctx = build_context(args)
    reserved = ({c.strip() for c in args.reserved_capabilities.split(",") if c.strip()}
                if args.reserved_capabilities else None)
    try:
        res = generate(
            ctx,
            template_root=template_root_for(args),
            output_root=Path(args.output_root),
            force=args.force,
            dry_run=args.dry_run,
            reserved_capabilities=reserved,
        )
    except FileNotFoundError as e:
        print(f"[ERR] {e}", file=sys.stderr)
        return 1

    if res.refused:
        print(f"[REFUSED] {res.refused}", file=sys.stderr)
        return 2

    tag = " (dry-run)" if args.dry_run else ""
    print(f"generate-case{tag}: domain='{ctx['domain_folder']}' case='{ctx['case_name']}' "
          f"capability='{ctx['capability_key']}'")
    print(f"  output: {res.domain_dir}")
    for f in res.files:
        mark = {"created": "+", "upgraded": "~", "unchanged": "=", "conflict": "!", "skipped": "s"}.get(f.action, "?")
        extra = f"  ({f.detail})" if f.detail else ""
        print(f"  [{mark}] {f.rel}{extra}")
    if res.unknown_tokens:
        print(f"  [WARN] unknown template tokens left verbatim: {', '.join(sorted(res.unknown_tokens))}")
    if res.conflicts:
        print(f"  [CONFLICT] {res.conflicts} file(s) need manual resolution (diff3 markers written).",
              file=sys.stderr)
        return 2
    if not args.dry_run:
        print("  Next: review the seed feature in the gap-tracking MAPPING, then run "
              "`python scripts/update_capability_tracking.py` to regenerate tables + backlog.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
