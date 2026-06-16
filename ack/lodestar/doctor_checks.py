"""Lodestar doctor checks — the capability/gap-tracking preflight (opt-in).

These checks are *registered into* the generic doctor (:func:`ack.doctor.run_doctor`
via ``extra_checks``) only when the Lodestar plugin is enabled. They own everything
the generic core deliberately does not assume: the ``*-gap-tracking.md`` MAPPING
format, capability uniqueness, ``depends_on`` resolvability, the KGC-535 "buildable
type must carry a capability" rule, generated skills, and the generator plan dry-run.

Each check is a ``Callable[[DoctorContext], list[Finding]]``. They compose through
the shared context: :func:`check_gap_mappings` populates ``ctx.mappings`` /
``ctx.known_caps`` early, and the later checks read them. Checks degrade to SKIP/WARN
when their inputs are absent (no gap-tracking yet, no template tree ported) — never a
hard crash.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from ..doctor import (
    Check,
    DoctorContext,
    Finding,
    err,
    ok,
    rel,
    skip,
    syntax_error,
    warn,
)
from .spec import CAPABILITY_REQUIRED_TYPES

# Gap-tracking MAPPING format (Lodestar owns this convention).
VALID_PHASES = {"MVP", "V1", "V2", "V3", "out-of-scope", "-"}
VALID_TIERS = {"high", "medium", "low", "-"}
REQUIRED_FEATURE_KEYS = ("key", "feature", "phase", "tier")
_MAPPING_RE = re.compile(
    r"<!-- MAPPING-START -->\s*```json\s*(.*?)\s*```\s*<!-- MAPPING-END -->", re.DOTALL
)
_DOMAIN_FM_RE = re.compile(r"^domain:\s*(\S+)", re.MULTILINE)


def _research_dir(ctx: DoctorContext) -> Path:
    """Where gap-tracking domains live (Lodestar's default workspace convention)."""
    return ctx.root / "vault" / "Research"


@dataclass
class DomainMapping:
    domain: str
    file: Path
    features: list[dict[str, Any]]
    keys: dict[str, dict[str, Any]]  # capability key -> feature


# --------------------------------------------------------------------------- #
# Check — Gap-tracking MAPPINGs (populates ctx.mappings + ctx.known_caps)
# --------------------------------------------------------------------------- #
def check_gap_mappings(ctx: DoctorContext) -> list[Finding]:
    chk = "gap-mapping"
    findings: list[Finding] = []
    research = _research_dir(ctx)
    files = sorted(research.glob("**/*-gap-tracking.md")) if research.is_dir() else []
    if not files:
        return [skip(chk, "no *-gap-tracking.md found — Lodestar tracking not in use here")]

    mappings: list[DomainMapping] = []
    for tf in files:
        r = rel(tf, ctx.root)
        text = tf.read_text(encoding="utf-8")
        dm = _DOMAIN_FM_RE.search(text)
        domain = dm.group(1).strip() if dm else tf.parent.name.lower()
        if ctx.only_domain and domain != ctx.only_domain:
            continue
        m = _MAPPING_RE.search(text)
        if not m:
            findings.append(warn(chk, "no MAPPING block — not a tracked domain", file=r))
            continue
        try:
            mapping = json.loads(m.group(1))
        except json.JSONDecodeError as e:
            findings.append(err(chk, f"MAPPING JSON is invalid: {e}", file=r,
                                fix="repair the JSON inside the MAPPING-START/END markers"))
            continue
        features = mapping.get("features", [])
        if not isinstance(features, list) or not features:
            findings.append(warn(chk, "MAPPING has no features", file=r))
            continue

        keys: dict[str, dict[str, Any]] = {}
        domain_ok = True
        for i, feat in enumerate(features):
            loc = f"features[{i}]"
            if not isinstance(feat, dict):
                findings.append(err(chk, "feature is not an object", file=r, field=loc))
                domain_ok = False
                continue
            missing = [k for k in REQUIRED_FEATURE_KEYS if not feat.get(k)]
            if missing:
                findings.append(err(chk, f"feature missing required key(s): {', '.join(missing)}",
                                    file=r, field=loc, fix=f"add {missing} to this feature"))
                domain_ok = False
            key = feat.get("key")
            if key:
                if key in keys:
                    findings.append(err(chk, f"duplicate capability key '{key}' within domain",
                                        file=r, field=loc, fix="make each feature key unique"))
                    domain_ok = False
                else:
                    keys[key] = feat
            phase = feat.get("phase", "-")
            if phase not in VALID_PHASES:
                findings.append(warn(chk, f"feature '{key}' has unknown phase '{phase}'",
                                     file=r, field=loc, fix=f"use one of {sorted(VALID_PHASES)}"))
            tier = feat.get("tier", "-")
            if tier not in VALID_TIERS:
                findings.append(warn(chk, f"feature '{key}' has unknown tier '{tier}'",
                                     file=r, field=loc, fix=f"use one of {sorted(VALID_TIERS)}"))
        if domain_ok:
            findings.append(ok(chk, f"domain '{domain}': {len(keys)} feature(s) valid", file=r))
        mappings.append(DomainMapping(domain, tf, features, keys))

    ctx.mappings = mappings
    for dm2 in mappings:
        ctx.known_caps |= set(dm2.keys)
    return findings


# --------------------------------------------------------------------------- #
# Check — Capability uniqueness across domains
# --------------------------------------------------------------------------- #
def check_capability_uniqueness(ctx: DoctorContext) -> list[Finding]:
    chk = "cap-unique"
    seen: dict[str, DomainMapping] = {}
    findings: list[Finding] = []
    collided = False
    for dm in ctx.mappings:
        for key in dm.keys:
            if key in seen and seen[key].domain != dm.domain:
                findings.append(
                    err(chk, f"capability key '{key}' is claimed by two domains "
                             f"('{seen[key].domain}' and '{dm.domain}')",
                        file=rel(dm.file, ctx.root),
                        fix=f"rename one — also defined in {rel(seen[key].file, ctx.root)}")
                )
                collided = True
            else:
                seen[key] = dm
    if not collided and seen:
        findings.append(ok(chk, f"{len(seen)} capability key(s) globally unique"))
    return findings


# --------------------------------------------------------------------------- #
# Check — Feature depends_on resolvability
# --------------------------------------------------------------------------- #
def check_depends_on(ctx: DoctorContext) -> list[Finding]:
    chk = "depends-on"
    known = ctx.known_caps
    done_ids = {r.task_id for r in ctx.records if r.bucket == "done"}
    findings: list[Finding] = []
    unresolved = 0
    for dm in ctx.mappings:
        for key, feat in dm.keys.items():
            for dep in feat.get("depends_on") or []:
                resolved = dep in known or (re.match(r"^[A-Z][A-Z0-9]*-", dep) and dep in done_ids)
                if not resolved:
                    findings.append(
                        warn(chk, f"feature '{key}' depends_on unresolved '{dep}'",
                             file=rel(dm.file, ctx.root),
                             fix="point at an existing capability key or a done task")
                    )
                    unresolved += 1
    if ctx.mappings and not unresolved:
        findings.append(ok(chk, "all feature depends_on resolve"))
    return findings


# --------------------------------------------------------------------------- #
# Check — TaskStore capability rule (KGC-535) + capability resolution
# --------------------------------------------------------------------------- #
def check_taskstore_capability(ctx: DoctorContext) -> list[Finding]:
    chk = "cap-rule"
    findings: list[Finding] = []
    known = ctx.known_caps
    buildable = {t.value for t in CAPABILITY_REQUIRED_TYPES}
    content = ctx.records if ctx.include_done else [r for r in ctx.records if r.bucket != "done"]

    cap_missing = 0
    cap_typo = 0
    for r in content:
        rl = rel(r.file, ctx.root)
        ttype = r.data.get("type")
        cap = (r.data.get("capability") or "").strip()

        if ttype in buildable and not cap:
            sev = warn if r.bucket == "done" else err
            findings.append(
                sev(chk, f"{r.task_id}: type='{ttype}' is buildable but has no 'capability'",
                    file=rl, field="capability",
                    fix="add the capability key so tracking stays drift-free")
            )
            cap_missing += 1

        if cap and known and cap not in known:
            findings.append(
                warn(chk, f"{r.task_id}: capability '{cap}' matches no gap-tracking MAPPING",
                     file=rl, field="capability",
                     fix="fix the typo or add the feature to a domain MAPPING")
            )
            cap_typo += 1

    scope = "all" if ctx.include_done else "pending/in_progress"
    if content and not cap_missing:
        findings.append(ok(chk, f"every buildable-type task ({scope}) carries a capability"))
    if known and content and not cap_typo:
        findings.append(ok(chk, f"every task capability ({scope}) resolves to a MAPPING"))
    return findings


# --------------------------------------------------------------------------- #
# Check — Skill registry (generated cases)
# --------------------------------------------------------------------------- #
def check_skill_registry(ctx: DoctorContext) -> list[Finding]:
    chk = "skills"
    findings: list[Finding] = []
    known = ctx.known_caps
    research = _research_dir(ctx)
    skill_dirs = [d for d in (research.glob("**/skills") if research.is_dir() else []) if d.is_dir()]
    if not skill_dirs:
        return [skip(chk, "no generated skills/ directories yet")]

    seen_caps: dict[str, str] = {}
    for sdir in skill_dirs:
        for py in sorted(sdir.glob("*.py")):
            if py.name.startswith("_"):
                continue
            r = rel(py, ctx.root)
            syn = syntax_error(py)
            if syn:
                findings.append(err(chk, f"skill module does not compile: {syn}", file=r,
                                    fix="fix the syntax error"))
                continue
            try:
                from ..doctor import load_module_by_path
                mod = load_module_by_path(f"ack_skill_{py.stem}", py)
            except Exception as e:
                findings.append(err(chk, f"skill module failed to import: {e}", file=r))
                continue
            case = getattr(mod, "CASE", None)
            if not isinstance(case, dict) or not case.get("capability"):
                findings.append(err(chk, "skill exposes no CASE dict with a 'capability'", file=r,
                                    field="CASE", fix="define CASE = {'capability': '<key>', ...}"))
                continue
            cap = case["capability"]
            if cap in seen_caps:
                findings.append(err(chk, f"duplicate skill capability '{cap}'", file=r,
                                    field="CASE.capability", fix=f"also declared in {seen_caps[cap]}"))
            seen_caps[cap] = r
            if known and cap not in known:
                findings.append(err(chk, f"orphan skill: capability '{cap}' is in no MAPPING",
                                    file=r, field="CASE.capability",
                                    fix="add the feature to a domain gap-tracking MAPPING"))
    if seen_caps and not any(f.severity.value == "ERROR" for f in findings):
        findings.append(ok(chk, f"{len(seen_caps)} skill(s) resolve to a capability"))
    return findings


# --------------------------------------------------------------------------- #
# Check — Template tree + plan dry-run (the generator scaffolding)
# --------------------------------------------------------------------------- #
def _template_root() -> Optional[Path]:
    try:
        from .. import generator as gen
    except Exception:
        return None
    tr = gen.DEFAULT_TEMPLATE
    return tr if tr.is_dir() else None


def check_template_tree(ctx: DoctorContext) -> list[Finding]:
    chk = "template"
    tr = _template_root()
    if tr is None:
        return [skip(chk, "no generator template tree present (templates not ported yet)")]
    findings: list[Finding] = []
    if not (tr / "copier.yml").is_file():
        findings.append(warn(chk, "template is missing 'copier.yml'", file=tr.name,
                             fix="add copier.yml to the template root"))
    bad = 0
    for py in sorted(tr.rglob("*.py")):
        if "__pycache__" in py.parts:
            continue
        syn = syntax_error(py)
        if syn:
            findings.append(err(chk, f"template .py does not compile: {syn}", file=py.name,
                                fix="keep {{tokens}} inside string literals only"))
            bad += 1
    if not bad and not any(f.severity.value == "ERROR" for f in findings):
        findings.append(ok(chk, "generator template tree intact and compiles"))
    return findings


def check_plan_dry_run(ctx: DoctorContext) -> list[Finding]:
    chk = "plan-dry-run"
    if not ctx.do_dry_run:
        return [skip(chk, "skipped (--no-dry-run)")]
    tr = _template_root()
    if tr is None:
        return [skip(chk, "no template tree — plan gate skipped")]
    try:
        from .. import generator as gen
    except Exception as e:
        return [err(chk, f"generator failed to import: {e}")]

    import tempfile
    from types import SimpleNamespace

    findings: list[Finding] = []
    domains = sorted({dm.domain for dm in ctx.mappings}) or ["demo"]
    failed = 0
    with tempfile.TemporaryDirectory(prefix="ack-doctor-") as tmp:
        out_root = Path(tmp)
        for domain in domains:
            args = SimpleNamespace(
                domain=domain, case="doctor-probe", description="doctor preflight probe",
                prefix=None, phase="MVP", tier="high", type="implementation",
                assignee="claude-opus-4-8", effort="high", non_negotiable=False, tags=None,
            )
            try:
                ctx_render = gen.build_context(args)
                res = gen.generate(ctx_render, template_root=tr, output_root=out_root, dry_run=True)
            except Exception as e:
                findings.append(err(chk, f"dry-run render failed for domain '{domain}': {e}"))
                failed += 1
                continue
            if getattr(res, "unknown_tokens", None):
                findings.append(err(chk, f"domain '{domain}': unknown template token(s) "
                                         f"{sorted(res.unknown_tokens)}",
                                     fix="define the token in build_context() or fix the template"))
                failed += 1
    if not failed:
        findings.append(ok(chk, f"plan dry-run renders {len(domains)} domain(s) with no unknown token"))
    return findings


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #
def lodestar_checks() -> list[Check]:
    """The ordered Lodestar checks to pass to :func:`ack.doctor.run_doctor` as
    ``extra_checks`` when the plugin is enabled. Order matters: gap-mappings runs
    first to populate ``ctx.mappings`` / ``ctx.known_caps`` for the rest."""
    return [
        check_gap_mappings,
        check_capability_uniqueness,
        check_depends_on,
        check_taskstore_capability,
        check_skill_registry,
        check_template_tree,
        check_plan_dry_run,
    ]
