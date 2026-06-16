#!/usr/bin/env python3
"""Lodestar capability tracking — gap-tracking → status tables + backlog generation.

The *generation* half of the Lodestar plugin (the doctor only *validates* the format;
this **regenerates** the derived artefacts). A capability domain is one
``<name>-gap-tracking.md`` carrying a JSON MAPPING block under ``vault/Research/``.
For every such domain this:

  1. discovers all ``*-gap-tracking.md`` with a MAPPING block (auto-discovery),
  2. computes each feature's status from the TaskStore,
  3. regenerates the status tables in place (between ``<!-- TABLES-START/END -->``),
  4. writes the sibling backlog (``<name>-backlog.md``): open gaps, rank-ordered,
     depends_on-gated, with handover seeds for the orchestrator.

DRIFT-FREE JOIN: tasks carry ``capability: "<key>"``. Per domain only tasks whose key
exists in *that* domain's MAPPING are joined (no cross-domain collision). A new domain
= drop a new ``*-gap-tracking.md`` — no code change.

Generic & workspace-rooted: paths derive from the passed ``root``; the Task-ID prefix
is generic (any ``PREFIX-N``). Opt-in: only meaningful when the Lodestar plugin is in
use. Stdlib only.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

PHASE_ORDER = {"MVP": 0, "V1": 1, "V2": 2, "V3": 3, "out-of-scope": 4, "-": 5}
TIER_ORDER = {"high": 0, "medium": 1, "low": 2, "-": 3}

# Status labels (the emoji prefix is the machine-checked part).
IMPLEMENTED = "✅ implemented"
PARTIAL = "🟡 partial"
IN_PROGRESS = "⏳ in-progress"
NOT_STARTED = "🔴 not-started"
OUT_OF_SCOPE = "⚪ out-of-scope"

_MAPPING_RE = re.compile(
    r"<!-- MAPPING-START -->\s*```json\s*(.*?)\s*```\s*<!-- MAPPING-END -->", re.DOTALL
)
#: Generic Task-ID stem (any uppercase prefix + number, optional ``-A`` variant).
_ID_RE = re.compile(r"^[A-Z][A-Z0-9]*-\d+(?:-[A-Z])?$")
_ID_PREFIX_RE = re.compile(r"^[A-Z][A-Z0-9]*-\d")
_DOMAIN_FM_RE = re.compile(r"^domain:\s*(\S+)", re.MULTILINE)
_TITLE_FM_RE = re.compile(r'^title:\s*"?(.*?)"?\s*$', re.MULTILINE)


# --------------------------------------------------------------------------- #
# TaskStore join
# --------------------------------------------------------------------------- #
def task_index(tasks_dir: Path) -> tuple[dict[str, str], dict[str, dict[str, list[str]]], set[str]]:
    """``bucket_by_id``: Task-ID -> bucket. ``cap_by_key``: capability-key ->
    {bucket: [ids]}. ``seen_keys``: every capability key present on a task."""
    bucket_by_id: dict[str, str] = {}
    cap_by_key: dict[str, dict[str, list[str]]] = {}
    seen_keys: set[str] = set()
    for bucket in ("done", "in_progress", "pending"):
        d = tasks_dir / bucket
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.json")):
            if not _ID_RE.match(f.stem):
                continue
            tid = f.stem
            bucket_by_id[tid] = bucket
            try:
                raw = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            cap = raw.get("capability")
            if cap:
                seen_keys.add(cap)
                cap_by_key.setdefault(cap, {}).setdefault(bucket, []).append(tid)
    return bucket_by_id, cap_by_key, seen_keys


def effective_ids(feat: dict[str, Any], cap_by_key: dict[str, dict[str, list[str]]]) -> list[str]:
    ids = list(feat.get("task_ids", []))
    for bucket_ids in cap_by_key.get(feat["key"], {}).values():
        ids.extend(bucket_ids)
    seen, out = set(), []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def compute_status(feat: dict[str, Any], bucket_by_id: dict[str, str],
                   cap_by_key: dict[str, dict[str, list[str]]]) -> str:
    if feat.get("phase") == "out-of-scope" or feat.get("no_code"):
        # no_code guard: a feature with no_code=True has no code even with a done
        # task → stays out-of-scope (prevents false positives).
        return OUT_OF_SCOPE
    ids = effective_ids(feat, cap_by_key)
    if not ids:
        return NOT_STARTED
    states = [bucket_by_id.get(k) for k in ids]
    if any(s in ("in_progress", "pending") for s in states) and not all(s == "done" for s in states):
        return IN_PROGRESS
    if all(s == "done" for s in states):
        if feat.get("status_when_done") == "partial":
            if cap_by_key.get(feat["key"], {}).get("done"):
                return IMPLEMENTED
            return PARTIAL
        return IMPLEMENTED
    if any(s == "done" for s in states):
        return PARTIAL
    return NOT_STARTED


def md_escape(s: Any) -> str:
    return str(s).replace("|", "\\|")


def gap_rank(feat: dict[str, Any]) -> tuple[int, int, int]:
    nn = 0 if feat.get("non_negotiable") else 1
    return (PHASE_ORDER.get(feat.get("phase", "-"), 9), nn, TIER_ORDER.get(feat.get("tier", "-"), 9))


def type_for(feat: dict[str, Any]) -> str:
    """Explicit ``type`` in the MAPPING wins; else a keyword heuristic over notes."""
    if feat.get("type"):
        return feat["type"]
    note = (feat.get("notes", "") + feat.get("feature", "")).lower()
    if any(w in note for w in ["security", "sandbox", "signing", "egress", "rls",
                               "audit", "crypto", "credential"]):
        return "security"
    return "implementation"


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def build_tables(features: list[dict[str, Any]], bucket_by_id: dict[str, str],
                 cap_by_key: dict[str, dict[str, list[str]]], today: str) -> str:
    rows = []
    for feat in features:
        st = compute_status(feat, bucket_by_id, cap_by_key)
        rows.append({"feature": feat["feature"], "phase": feat.get("phase", "-"), "status": st,
                     "ids": ", ".join(effective_ids(feat, cap_by_key)) or "—",
                     "nn": "🔒" if feat.get("non_negotiable") else "",
                     "sources": ", ".join(feat.get("sources", [])), "notes": feat.get("notes", "")})
    rows.sort(key=lambda r: (PHASE_ORDER.get(r["phase"], 9), r["feature"].lower()))
    c = lambda p: sum(1 for r in rows if r["status"].startswith(p))
    out = [
        f"*Auto-generated {today} via ack.lodestar.tracking — do not edit by hand.*", "",
        "### Metrics", "", "| Status | Count |", "|--------|-------|",
        f"| ✅ implemented | {c('✅')} |", f"| 🟡 partial | {c('🟡')} |", f"| ⏳ in-progress | {c('⏳')} |",
        f"| 🔴 not-started | {c('🔴')} |", f"| ⚪ out-of-scope | {c('⚪')} |", f"| **Total** | **{len(rows)}** |", "",
        "### Full feature matrix", "", "| Feature | Phase | Status | Tasks | NN | Sources |",
        "|---------|-------|--------|-------|----|---------|",
    ]
    for r in rows:
        out.append(f"| {md_escape(r['feature'])} | {r['phase']} | {r['status']} | {r['ids']} | {r['nn']} | {r['sources']} |")
    gaps = [r for r in rows if r["status"][0] in "🔴🟡"]
    out += ["", "### Open gaps & partial implementations", ""]
    if not gaps:
        out.append("*No open gaps.*")
    else:
        out += ["| Feature | Phase | Status | NN | Gap / next step |", "|---------|-------|--------|----|-----------------|"]
        for r in gaps:
            out.append(f"| {md_escape(r['feature'])} | {r['phase']} | {r['status']} | {r['nn']} | {md_escape(r['notes'])} |")
    out += ["", "> NN legend: 🔒 = non-negotiable."]
    return "\n".join(out)


def deps_satisfied(feat: dict[str, Any], bucket_by_id: dict[str, str], implemented_keys: set[str]) -> bool:
    """True iff every ``depends_on`` is an implemented capability key OR a done task."""
    for dep in (feat.get("depends_on") or []):
        if dep in implemented_keys:
            continue
        if _ID_PREFIX_RE.match(dep) and bucket_by_id.get(dep) == "done":
            continue
        return False
    return True


def build_backlog(features: list[dict[str, Any]], bucket_by_id: dict[str, str],
                  cap_by_key: dict[str, dict[str, list[str]]], domain_key: str,
                  title: str, tracking_stem: str, today: str) -> str:
    implemented_keys = {
        f["key"] for f in features
        if compute_status(f, bucket_by_id, cap_by_key).startswith("✅")
    }
    ready, blocked = [], []
    for f in features:
        s = compute_status(f, bucket_by_id, cap_by_key)
        if s[0] not in "🔴🟡":
            continue
        (ready if deps_satisfied(f, bucket_by_id, implemented_keys) else blocked).append((f, s))
    open_gaps = sorted(ready, key=lambda fs: gap_rank(fs[0]))
    nn_open = sum(1 for f, _ in open_gaps if f.get("non_negotiable"))
    lines = [
        "---", "type: backlog", f'title: "{title} — Planning Backlog"', f"date: {today}", f"updated: {today}",
        "author: ack.lodestar.tracking (auto)", f"domain: {domain_key}",
        f"tags: [backlog, {domain_key}, planning]", "---", "",
        f"# {title} — Planning Backlog", "",
        "> **Auto-generated — do not edit by hand.** Source: the MAPPING in",
        f"> [[{tracking_stem}|{tracking_stem}.md]] + the TaskStore.",
        "> Order: phase MVP→V1→V2→V3 → non-negotiable (🔒) → tier.",
        ">",
        "> **🟡 partial = OPEN, not done** — the remaining scope IS the task. Do not skip.",
        "> Always take the **top** entry (whether partial or not-started).",
        "",
        "## For the orchestrator (planning & creation)",
        "",
        "1. **Take the TOP entry** (rank #1). Deviate only with an operator reason.",
        "2. Create the handover + task via `stage_handover` — use the seed below.",
        "3. **Required:** set `capability: \"<key>\"` in the task JSON → drift-free status.",
        "4. Codebase paths ONLY from `anchors` or verified via search — never guessed.",
        "",
        f"**Open total:** {len(open_gaps)} · of which non-negotiable: {nn_open}", "", "---", "",
    ]
    if not open_gaps:
        lines.append("*No open gaps — goal reached (within the defined scope).* 🎉")
    else:
        for i, (feat, st) in enumerate(open_gaps, 1):
            nn = " · 🔒 **non-negotiable**" if feat.get("non_negotiable") else ""
            srcs = " · ".join(feat.get("sources", []))
            lines += [
                f"### {i}. `{feat['key']}` — {feat['feature']}",
                f"- **Status:** {st} · **Phase:** {feat.get('phase','-')} · **Tier:** {feat.get('tier','-')}{nn}",
                f"- **Proposal:** `type={type_for(feat)}` · `effort={feat.get('effort','high')}` · `assigned_to={feat.get('assignee','claude-opus-4-8')}`",
                f"- **Scope / gap:** {feat.get('notes','')}",
            ]
            if feat.get("anchors"):
                lines.append("- **Existing code (EXTEND, do not rebuild):** "
                             + ", ".join(f"`{a}`" for a in feat["anchors"]))
            lines += [f"- **Sources:** {srcs}" if srcs else "",
                      f"- **Required task field:** `\"capability\": \"{feat['key']}\"`", ""]
    if blocked:
        lines += ["---", "", "## ⏸ Blocked (depends_on unmet — not yet plannable)", ""]
        for f, _ in sorted(blocked, key=lambda fs: gap_rank(fs[0])):
            missing = [d for d in (f.get("depends_on") or [])
                       if d not in implemented_keys
                       and not (_ID_PREFIX_RE.match(d) and bucket_by_id.get(d) == "done")]
            lines.append(f"- `{f['key']}` — {f['feature']} · **waiting on:** {', '.join(missing)}")
        lines.append("")
    lines += ["---", "", "## See also", f"- [[{tracking_stem}|Gap-tracking (full matrix)]]", ""]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Per-domain processing + entry points
# --------------------------------------------------------------------------- #
def process_domain(tracking_path: Path, bucket_by_id: dict[str, str],
                   cap_by_key: dict[str, dict[str, list[str]]], today: str) -> Optional[dict[str, Any]]:
    text = tracking_path.read_text(encoding="utf-8")
    m = _MAPPING_RE.search(text)
    if not m:
        return None
    try:
        mapping = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        return {"error": f"{tracking_path.name}: MAPPING JSON invalid: {e}"}
    features = mapping.get("features", [])
    if not features:
        return None
    known = {f["key"] for f in features}
    dk = _DOMAIN_FM_RE.search(text)
    domain_key = dk.group(1).strip() if dk else tracking_path.parent.name.lower()
    tt = _TITLE_FM_RE.search(text)
    title = (tt.group(1).replace(" — Gap-Tracking", "").strip() if tt else tracking_path.parent.name)
    tracking_stem = tracking_path.stem

    # Regenerate the tables in place (only if the workspace carries the markers).
    tables = build_tables(features, bucket_by_id, cap_by_key, today)
    if "<!-- TABLES-START -->" in text:
        new_text = re.sub(r"<!-- TABLES-START -->.*?<!-- TABLES-END -->",
                          f"<!-- TABLES-START -->\n{tables}\n<!-- TABLES-END -->", text, flags=re.DOTALL)
        new_text = re.sub(r"(^updated: ).+", rf"\g<1>{today}", new_text, count=1, flags=re.MULTILINE)
        tracking_path.write_text(new_text, encoding="utf-8")

    # Write the sibling backlog.
    backlog_path = tracking_path.with_name(tracking_path.name.replace("-gap-tracking", "-backlog"))
    backlog = build_backlog(features, bucket_by_id, cap_by_key, domain_key, title, tracking_stem, today)
    backlog_path.write_text(backlog, encoding="utf-8")

    st = lambda p: sum(1 for f in features if compute_status(f, bucket_by_id, cap_by_key).startswith(p))
    return {"domain": domain_key, "features": len(features), "implemented": st("✅"),
            "partial": st("🟡"), "not_started": st("🔴"), "known": known, "backlog": backlog_path.name}


def run(root: Path, *, today: Optional[str] = None) -> dict[str, Any]:
    """Regenerate tables + backlog for every gap-tracking domain under *root*.

    ``today`` defaults to the current date; pass an explicit value for deterministic
    output (e.g. in tests). Returns a summary dict (domains processed, unknown keys)."""
    if today is None:
        from datetime import date
        today = str(date.today())
    research = root / "vault" / "Research"
    tasks_dir = root / "tasks"
    bucket_by_id, cap_by_key, seen_keys = task_index(tasks_dir)
    tracking_files = sorted(research.glob("**/*-gap-tracking.md")) if research.is_dir() else []
    domains, errors, all_known = [], [], set()
    for tf in tracking_files:
        res = process_domain(tf, bucket_by_id, cap_by_key, today)
        if not res:
            continue
        if "error" in res:
            errors.append(res["error"])
            continue
        all_known |= res["known"]
        domains.append(res)
    unknown = sorted(seen_keys - all_known)
    return {"domains": domains, "errors": errors, "unknown_capabilities": unknown}


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="ack.lodestar.tracking",
                                description="Regenerate gap-tracking tables + backlog from the TaskStore.")
    p.add_argument("--root", default=".", help="Workspace root (default: cwd)")
    args = p.parse_args(argv)
    res = run(Path(args.root).resolve())
    for d in res["domains"]:
        print(f"  [OK] {d['domain']}: {d['features']} features → "
              f"{d['implemented']}implemented {d['partial']}partial {d['not_started']}not-started · {d['backlog']}")
    for e in res["errors"]:
        print(f"  [ERR] {e}")
    if res["unknown_capabilities"]:
        print(f"  [WARN] task capability keys with no MAPPING entry: {', '.join(res['unknown_capabilities'])}")
    if not res["domains"]:
        print("  (no *-gap-tracking.md found)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
