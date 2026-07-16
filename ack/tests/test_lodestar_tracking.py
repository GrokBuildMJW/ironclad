"""Lodestar capability-tracking tests — status join, table regen, backlog generation."""
from __future__ import annotations

import json
import re

from ack.lodestar import tracking

TODAY = "2026-06-16"

MAPPING = {
    "features": [
        {"key": "feat-impl", "feature": "Implemented feature", "phase": "MVP", "tier": "high"},
        {"key": "feat-open", "feature": "Open feature", "phase": "MVP", "tier": "high",
         "non_negotiable": True, "notes": "todo"},
        {"key": "feat-ready", "feature": "Ready dep met", "phase": "V1", "tier": "medium",
         "depends_on": ["feat-impl"]},
        {"key": "feat-blocked", "feature": "Blocked dep unmet", "phase": "V1", "tier": "low",
         "depends_on": ["feat-open"]},
    ]
}

GAP = (
    "---\n"
    "domain: demo-domain\n"
    'title: "Demo — Gap-Tracking"\n'
    "updated: 2000-01-01\n"
    "---\n"
    "# Demo\n"
    "<!-- MAPPING-START -->\n"
    "```json\n" + json.dumps(MAPPING) + "\n```\n"
    "<!-- MAPPING-END -->\n\n"
    "## Status\n"
    "<!-- TABLES-START -->\nOLD CONTENT\n<!-- TABLES-END -->\n"
)


def _workspace(root):
    for bucket in ("pending", "in_progress", "done"):
        (root / "tasks" / bucket).mkdir(parents=True)
    # one done task implementing feat-impl
    (root / "tasks" / "done" / "PROJ-1.json").write_text(
        json.dumps({"type": "implementation", "priority": "high",
                    "title": "x", "description": "y", "capability": "feat-impl"}),
        encoding="utf-8",
    )
    domain_dir = root / "vault" / "Research" / "Demo"
    domain_dir.mkdir(parents=True)
    gap = domain_dir / "demo-gap-tracking.md"
    gap.write_text(GAP, encoding="utf-8")
    return gap


def test_status_join_from_taskstore(tmp_path):
    _workspace(tmp_path)
    res = tracking.run(tmp_path, today=TODAY)
    dom = res["domains"][0]
    assert dom["domain"] == "demo-domain"
    assert dom["implemented"] == 1          # feat-impl (done task)
    assert dom["not_started"] == 3          # feat-open / feat-ready / feat-blocked
    assert res["unknown_capabilities"] == []


def test_tables_regenerated_in_place(tmp_path):
    gap = _workspace(tmp_path)
    tracking.run(tmp_path, today=TODAY)
    text = gap.read_text(encoding="utf-8")
    assert "OLD CONTENT" not in text                 # replaced
    assert f"Auto-generated {TODAY}" in text
    assert "✅ implemented" in text                   # feat-impl status rendered
    assert "updated: 2026-06-16" in text             # frontmatter date bumped
    # MAPPING block is left intact (only the TABLES region is regenerated)
    assert "<!-- MAPPING-START -->" in text and "feat-impl" in text


def test_backlog_ready_blocked_and_ranking(tmp_path):
    _workspace(tmp_path)
    tracking.run(tmp_path, today=TODAY)
    backlog = (tmp_path / "vault" / "Research" / "Demo" / "demo-backlog.md").read_text(encoding="utf-8")

    open_section, _, blocked_section = backlog.partition("## ⏸ Blocked")

    # Ready entries appear in rank order: feat-open (NN/MVP) before feat-ready (V1).
    order = re.findall(r"^### \d+\. `([^`]+)`", open_section, re.M)
    assert order == ["feat-open", "feat-ready"]

    # Implemented feature is excluded from the open backlog entirely.
    assert "feat-impl" not in open_section

    # Blocked feature is parked with its unmet dependency (feat-open).
    assert "feat-blocked" in blocked_section
    assert "waiting on" in blocked_section and "feat-open" in blocked_section

    # Required capability field is advertised for each open entry.
    assert '"capability": "feat-open"' in open_section


def _gap(domain: str, mapping: dict) -> str:
    return (
        "---\n"
        f"domain: {domain}\n"
        f'title: "{domain} — Gap-Tracking"\n'
        "updated: 2000-01-01\n"
        "---\n"
        f"# {domain}\n"
        "<!-- MAPPING-START -->\n"
        "```json\n" + json.dumps(mapping) + "\n```\n"
        "<!-- MAPPING-END -->\n\n"
        "## Status\n"
        "<!-- TABLES-START -->\nOLD\n<!-- TABLES-END -->\n"
    )


def test_cross_domain_dependency_is_satisfied_when_implemented_elsewhere(tmp_path):
    # #1534: cap-b (domain b) depends on cap-a, implemented by a done task in domain a. The capability space
    # is global (like the doctor's resolution), so cap-b must be READY — not parked in b's Blocked section
    # forever just because cap-a is not a *local* feature.
    for bucket in ("pending", "in_progress", "done"):
        (tmp_path / "tasks" / bucket).mkdir(parents=True)
    (tmp_path / "tasks" / "done" / "A-1.json").write_text(
        json.dumps({"type": "implementation", "priority": "high",
                    "title": "x", "description": "y", "capability": "cap-a"}), encoding="utf-8")
    research = tmp_path / "vault" / "Research"
    (research / "A").mkdir(parents=True)
    (research / "B").mkdir(parents=True)
    (research / "A" / "a-gap-tracking.md").write_text(
        _gap("a", {"features": [{"key": "cap-a", "feature": "A", "phase": "MVP"}]}), encoding="utf-8")
    (research / "B" / "b-gap-tracking.md").write_text(
        _gap("b", {"features": [{"key": "cap-b", "feature": "B", "phase": "MVP", "depends_on": ["cap-a"]}]}),
        encoding="utf-8")

    tracking.run(tmp_path, today=TODAY)
    backlog_b = (research / "B" / "b-backlog.md").read_text(encoding="utf-8")
    open_section, _, blocked_section = backlog_b.partition("## ⏸ Blocked")
    order = re.findall(r"^### \d+\. `([^`]+)`", open_section, re.M)
    assert "cap-b" in order                       # ready: its cross-domain dependency is implemented
    assert "cap-b" not in blocked_section         # was: parked as blocked, waiting on cap-a forever
