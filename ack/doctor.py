#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Doctor — ACK workspace preflight (generic core).

The ``terraform validate`` / ``kubectl --dry-run`` of the Agent-Contract-Kernel:
a deterministic, **read-only** preflight that validates a workspace before a run and
**fails loud** with *file + field + fix* instead of letting a forgotten key blow up
three steps downstream at runtime.

This is the **generic** core. It ships three layout-agnostic checks:

  1. **ACK kernel schema** — the SSOT spec (:mod:`ack.case_spec`) loads, its JSON-
     Schema is XGrammar-clean, and the canonical example round-trips.
  2. **TaskStore integrity** — no duplicate Task-ID across status dirs (the reconciler
     cannot disambiguate); every ``dependencies`` entry resolves to an existing task.
  3. **Deps / MCP** — ``.mcp.json`` parses and referenced commands/scripts exist;
     pydantic importable. Fail-SOFT (warnings).

Opinionated, layout-specific checks (gap-tracking MAPPINGs, capability uniqueness,
the KGC-535 capability rule, generated skills, the generator plan dry-run) are NOT
here — they belong to the **Lodestar** plugin and are *registered* into this doctor
via :func:`run_doctor`'s ``extra_checks`` when Lodestar is enabled. A check is just a
``Callable[[DoctorContext], list[Finding]]``; the context carries shared state
(records, plugin-populated mappings/known_caps) so plugin checks compose cleanly.

POSTURE: strictly **read-only** (a preflight never mutates the workspace). Zero
third-party deps beyond the optional pydantic (its absence degrades the kernel check
to an explicit SKIP, never a crash).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

TASK_BUCKETS = ("pending", "in_progress", "done")
#: Generic Task-ID stem (any uppercase prefix + number, optional ``-A`` variant).
_TASK_STEM_RE = re.compile(r"^[A-Z][A-Z0-9]*-\d+(?:-[A-Z])?$")


# --------------------------------------------------------------------------- #
# Findings
# --------------------------------------------------------------------------- #
class Severity(str, Enum):
    OK = "OK"
    WARN = "WARN"
    ERROR = "ERROR"
    SKIP = "SKIP"


@dataclass(frozen=True)
class Finding:
    """One preflight result. ``file`` + ``field`` + ``fix`` are the fail-loud trio."""

    check: str
    severity: Severity
    message: str
    file: Optional[str] = None
    field: Optional[str] = None
    fix: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "severity": self.severity.value,
            "message": self.message,
            "file": self.file,
            "field": self.field,
            "fix": self.fix,
        }

    def render(self) -> str:
        loc = ""
        if self.file:
            loc = f" [{self.file}" + (f":{self.field}" if self.field else "") + "]"
        line = f"{self.message}{loc}"
        if self.fix and self.severity in (Severity.ERROR, Severity.WARN):
            line += f"\n         fix: {self.fix}"
        return line


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)

    def add(self, f: Finding) -> None:
        self.findings.append(f)

    def extend(self, fs: Iterable[Finding]) -> None:
        self.findings.extend(fs)

    def count(self, sev: Severity) -> int:
        return sum(1 for f in self.findings if f.severity is sev)

    def has_errors(self, *, strict: bool = False) -> bool:
        if self.count(Severity.ERROR):
            return True
        return strict and self.count(Severity.WARN) > 0


# Convenience constructors keep the check bodies terse.
def ok(check: str, message: str, **kw: Any) -> Finding:
    return Finding(check, Severity.OK, message, **kw)


def warn(check: str, message: str, **kw: Any) -> Finding:
    return Finding(check, Severity.WARN, message, **kw)


def err(check: str, message: str, **kw: Any) -> Finding:
    return Finding(check, Severity.ERROR, message, **kw)


def skip(check: str, message: str, **kw: Any) -> Finding:
    return Finding(check, Severity.SKIP, message, **kw)


def rel(p: Path, root: Path) -> str:
    try:
        return p.relative_to(root).as_posix()
    except ValueError:
        return str(p)


def syntax_error(path: Path) -> Optional[str]:
    """Read-only syntax check. Returns an error string, or None if the file parses.

    Uses the builtin :func:`compile` (no bytecode written) — a preflight must never
    mutate the workspace (``py_compile`` would litter ``__pycache__``)."""
    try:
        src = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return f"unreadable: {e}"
    try:
        compile(src, str(path), "exec")
    except SyntaxError as e:
        return f"{e.msg} (line {e.lineno})"
    return None


def load_module_by_path(name: str, path: Path):
    """Load a module directly from a file, bypassing its package ``__init__``.

    Used for hyphenated/standalone files (e.g. a vessel's skill modules) — the same
    loader pattern the generator's tests use."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclasses / pickling need the module registered
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------- #
# TaskStore index (shared by core + plugin checks via the context)
# --------------------------------------------------------------------------- #
@dataclass
class TaskRecord:
    task_id: str
    bucket: str
    file: Path
    data: dict[str, Any]


def index_tasks(root: Path) -> tuple[list[TaskRecord], list[Finding]]:
    """Read every ``tasks/<bucket>/*.json`` once. Returns records + parse findings."""
    records: list[TaskRecord] = []
    findings: list[Finding] = []
    tasks_dir = root / "tasks"
    for bucket in TASK_BUCKETS:
        d = tasks_dir / bucket
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.json")):
            if not _TASK_STEM_RE.match(f.stem):
                findings.append(
                    warn("taskstore", f"task file stem is not a valid Task-ID: {f.stem}",
                         file=rel(f, root), fix="rename to <PREFIX>-NNN.json")
                )
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                findings.append(
                    err("taskstore", f"task JSON does not parse: {e}",
                        file=rel(f, root), fix="repair the JSON syntax")
                )
                continue
            records.append(TaskRecord(f.stem, bucket, f, data))
    return records, findings


# --------------------------------------------------------------------------- #
# Doctor context + check protocol
# --------------------------------------------------------------------------- #
@dataclass
class DoctorContext:
    """Shared, mutable state threaded through every check.

    Core checks populate ``records`` / ``case_spec``. Plugin checks (Lodestar) may
    populate ``mappings`` / ``known_caps`` early so later checks compose on them —
    a downstream check that finds ``known_caps`` empty simply skips its
    capability-resolution sub-check.
    """

    root: Path
    records: list[TaskRecord] = field(default_factory=list)
    case_spec: Any = None  # the ack.case_spec module (or None if pydantic absent)
    validate_tasks: bool = False
    include_done: bool = False
    only_domain: Optional[str] = None
    do_dry_run: bool = True
    # plugin-populated (Lodestar):
    mappings: list = field(default_factory=list)
    known_caps: set = field(default_factory=set)
    extra: dict = field(default_factory=dict)


#: A check: pure, read-only, returns findings. May read/write the context.
Check = Callable[[DoctorContext], list[Finding]]


# --------------------------------------------------------------------------- #
# Core check 1 — ACK kernel schema (the SSOT)
# --------------------------------------------------------------------------- #
def check_ack_kernel(ctx: DoctorContext) -> list[Finding]:
    """Load the SSOT spec, assert the grammar is clean and the example round-trips.

    Imports :mod:`ack.case_spec` directly (it is pure pydantic+stdlib). Stores the
    module on ``ctx.case_spec`` for downstream checks. Degrades to SKIP if pydantic
    is not importable."""
    chk = "ack-kernel"
    try:
        import pydantic  # noqa: F401
    except ImportError:
        return [skip(chk, "pydantic not importable — kernel schema check skipped",
                     fix="pip install 'pydantic>=2' to enable the schema gate")]
    try:
        from . import case_spec as cs
    except Exception as e:  # import/exec error in the SSOT is a hard stop
        return [err(chk, f"ACK SSOT spec failed to import: {e}",
                    fix="fix the import/syntax error in ack/case_spec.py")]

    ctx.case_spec = cs
    findings: list[Finding] = []
    schema = cs.task_spec_json_schema()
    lint = cs.lint_schema_for_xgrammar(schema)
    if lint:
        for f in lint:
            findings.append(
                err(chk, f"schema carries XGrammar-unsupported keyword '{f.keyword}'",
                    field=f.path,
                    fix="move this rule into a Pydantic validator (it 400s under XGrammar V1)")
            )
    else:
        findings.append(ok(chk, "ACK SSOT schema is XGrammar-clean"))

    try:
        cs.validate_task_json(cs.EXAMPLE_TASK_JSON)
        findings.append(ok(chk, "canonical EXAMPLE_TASK_JSON validates against TaskSpec"))
    except Exception as e:
        findings.append(
            err(chk, f"canonical example no longer validates: {e}",
                fix="reconcile EXAMPLE_TASK_JSON with TaskSpec")
        )
    return findings


# --------------------------------------------------------------------------- #
# Core check 2 — TaskStore integrity (dup-ID + dependency resolution)
# --------------------------------------------------------------------------- #
def check_task_store_integrity(ctx: DoctorContext) -> list[Finding]:
    """Layout-agnostic TaskStore checks: a Task-ID lives in exactly one status dir,
    and every ``dependencies`` entry resolves to an existing task. (Capability /
    KGC-535 rules are Lodestar's — see :mod:`ack.lodestar.doctor_checks`.)"""
    chk = "taskstore"
    findings: list[Finding] = []
    records = ctx.records
    all_ids = {r.task_id for r in records}

    by_id: dict[str, list[TaskRecord]] = {}
    for r in records:
        by_id.setdefault(r.task_id, []).append(r)
    dup = False
    for tid, recs in by_id.items():
        if len(recs) > 1:
            where = ", ".join(rel(r.file, ctx.root) for r in recs)
            findings.append(
                err(chk, f"Task-ID '{tid}' appears {len(recs)}x: {where}",
                    fix="a Task-ID must live in exactly one status dir (reconciler relies on it)")
            )
            dup = True
    if not dup and records:
        findings.append(ok(chk, f"{len(by_id)} Task-ID(s) unique across status dirs"))

    content = records if ctx.include_done else [r for r in records if r.bucket != "done"]
    dep_missing = 0
    for r in content:
        for dep in r.data.get("dependencies") or []:
            if dep not in all_ids:
                findings.append(
                    warn(chk, f"{r.task_id}: dependency '{dep}' is not a known Task-ID",
                         file=rel(r.file, ctx.root), field="dependencies",
                         fix="point at an existing task or drop the dependency")
                )
                dep_missing += 1
    if records and not dep_missing:
        findings.append(ok(chk, "all task dependencies resolve to existing tasks"))
    return findings


# --------------------------------------------------------------------------- #
# Core check 3 — Deps / MCP presence (fail-soft)
# --------------------------------------------------------------------------- #
_WIN_EXEC_EXTS = (".exe", ".cmd", ".bat", "")


def _exec_exists(path: str) -> bool:
    """True if *path* exists, tolerating a bare (extension-less) Windows command."""
    p = Path(path)
    if p.exists():
        return True
    if not p.suffix:
        return any((p.with_suffix(ext) if ext else p).exists() for ext in _WIN_EXEC_EXTS)
    return False


def check_deps_mcp(ctx: DoctorContext) -> list[Finding]:
    chk = "deps"
    root = ctx.root
    findings: list[Finding] = []
    try:
        import pydantic  # noqa: F401
        findings.append(ok(chk, "pydantic importable"))
    except ImportError:
        findings.append(warn(chk, "pydantic not importable — kernel schema gate degrades to SKIP",
                             fix="pip install 'pydantic>=2'"))

    mcp_config = root / ".mcp.json"
    if not mcp_config.is_file():
        findings.append(warn(chk, ".mcp.json not found — memory/MCP stack unconfigured",
                             file=rel(mcp_config, root), fix="add .mcp.json (fail-soft per policy)"))
        return findings
    try:
        cfg = json.loads(mcp_config.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return findings + [err(chk, f".mcp.json does not parse: {e}", file=rel(mcp_config, root),
                               fix="repair the JSON")]

    servers = cfg.get("mcpServers", {})
    if not servers:
        findings.append(warn(chk, ".mcp.json declares no mcpServers", file=rel(mcp_config, root)))
    for name, sv in servers.items():
        # Read paths only — never any secret value.
        targets = []
        cmd = sv.get("command")
        if cmd and ("/" in cmd or "\\" in cmd):
            targets.append(cmd)
        for a in sv.get("args", []):
            if isinstance(a, str) and a.lower().endswith(".py"):
                targets.append(a)
        for t in targets:
            if not _exec_exists(t):
                findings.append(
                    warn(chk, f"MCP server '{name}' references missing path: {t}",
                         file=rel(mcp_config, root), field=name,
                         fix="fix the path or install the missing runtime (fail-soft)")
                )
        if targets and all(_exec_exists(t) for t in targets):
            findings.append(ok(chk, f"MCP server '{name}' resolves ({len(targets)} path(s))"))
    return findings


#: The generic, always-on checks in dependency order.
CORE_CHECKS: list[Check] = [check_ack_kernel, check_task_store_integrity, check_deps_mcp]


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def run_doctor(
    root: Path,
    *,
    only_domain: Optional[str] = None,
    do_dry_run: bool = True,
    validate_tasks: bool = False,
    include_done: bool = False,
    extra_checks: Optional[Iterable[Check]] = None,
) -> Report:
    """Run the core checks plus any plugin-contributed ``extra_checks`` (e.g.
    Lodestar's, when enabled), in order, and return the aggregated report.

    The TaskStore is indexed once and shared via the context. ``check_ack_kernel``
    runs first so ``ctx.case_spec`` is available to later checks. Plugin checks are
    appended after the core kernel/index checks so they can read ``ctx.records`` and
    populate ``ctx.mappings`` / ``ctx.known_caps`` for one another."""
    ctx = DoctorContext(
        root=root,
        only_domain=only_domain,
        do_dry_run=do_dry_run,
        validate_tasks=validate_tasks,
        include_done=include_done,
    )
    report = Report()

    # Kernel first (populates ctx.case_spec).
    report.extend(check_ack_kernel(ctx))

    # Index the TaskStore once; shared by integrity + plugin capability checks.
    records, idx_findings = index_tasks(root)
    ctx.records = records
    report.extend(idx_findings)

    # Remaining core checks.
    report.extend(check_task_store_integrity(ctx))
    report.extend(check_deps_mcp(ctx))

    # Plugin checks (Lodestar etc.) — registered, ordered, share the context.
    for check in extra_checks or ():
        report.extend(check(ctx))

    return report


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
_SEV_MARK = {Severity.OK: "+", Severity.WARN: "!", Severity.ERROR: "x", Severity.SKIP: "."}


def render_report(report: Report, *, quiet: bool) -> str:
    lines: list[str] = []
    order: list[str] = []
    for f in report.findings:
        if f.check not in order:
            order.append(f.check)
    for check in order:
        fs = [f for f in report.findings if f.check == check]
        shown = fs if not quiet else [f for f in fs if f.severity in (Severity.WARN, Severity.ERROR)]
        if not shown:
            continue
        lines.append(f"\n[{check}]")
        for f in shown:
            lines.append(f"  {_SEV_MARK[f.severity]} {f.render()}")
    return "\n".join(lines)


def _load_lodestar_checks(enabled: bool) -> list[Check]:
    """Return Lodestar's doctor checks when the plugin is enabled, else none."""
    if not enabled:
        return []
    try:
        from .lodestar.doctor_checks import lodestar_checks
    except Exception:
        return []
    return lodestar_checks()


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="doctor",
        description="ACK preflight: validate the workspace, fail loud with file+field+fix.",
    )
    p.add_argument("--root", default=".", help="Workspace root (default: cwd)")
    p.add_argument("--domain", default=None, help="Limit MAPPING-scoped checks to one domain")
    p.add_argument("--json", action="store_true", help="Machine-readable output (CI)")
    p.add_argument("--strict", action="store_true", help="Treat warnings as errors")
    p.add_argument("--no-dry-run", action="store_true", help="Skip the generator plan gate")
    p.add_argument("--validate-tasks", action="store_true",
                   help="Run full TaskSpec validation on every task (opt-in)")
    p.add_argument("--all-tasks", action="store_true",
                   help="Extend per-task content checks to done/ (default: pending + in_progress)")
    p.add_argument("--lodestar", action="store_true",
                   help="Also run the Lodestar capability/gap-tracking checks")
    p.add_argument("--quiet", action="store_true", help="Print only warnings/errors")
    args = p.parse_args(argv)

    root = Path(args.root).resolve()
    try:
        report = run_doctor(
            root,
            only_domain=args.domain,
            do_dry_run=not args.no_dry_run,
            validate_tasks=args.validate_tasks,
            include_done=args.all_tasks,
            extra_checks=_load_lodestar_checks(args.lodestar),
        )
    except Exception as e:  # doctor itself broke — exit 1, distinct from a failed check
        print(f"[doctor] internal error: {e}", file=sys.stderr)
        return 1

    errors = report.count(Severity.ERROR)
    warns = report.count(Severity.WARN)
    oks = report.count(Severity.OK)
    failed = report.has_errors(strict=args.strict)

    if args.json:
        print(json.dumps({
            "ok": not failed,
            "summary": {"error": errors, "warn": warns, "ok": oks, "skip": report.count(Severity.SKIP)},
            "findings": [f.as_dict() for f in report.findings],
        }, indent=2, ensure_ascii=False))
    else:
        print("doctor — ACK workspace preflight")
        print(render_report(report, quiet=args.quiet))
        print(f"\nsummary: {oks} ok · {warns} warn · {errors} error"
              + (" · (strict)" if args.strict else ""))
        if failed:
            print("RESULT: FAILED — resolve the errors above before running.", file=sys.stderr)
        else:
            print("RESULT: All checks passed." + (" (warnings present)" if warns else ""))

    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
