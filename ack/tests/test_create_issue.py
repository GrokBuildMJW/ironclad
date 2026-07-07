"""#1073 (epic #1043): the secret-free, escape-free `create_issue` tool — CAPABILITY-DETECTED + hardened.

Lets the orchestrator FILE its own tracker issues (GitHub via the `gh` CLI). It is offered whenever its
capability is PRESENT (the `gh` CLI on PATH) — mirroring web_search/memory/etc., so the whole tool surface is
uniformly capability-detected rather than behind a manual opt-in flag (installing + authing gh IS the
operator's deliberate opt-in). The operator can still force it off (forge.enabled=false); it is blocked under
the sealed profile (no autonomous outbound writes). The body comes from a FILE (no giant JSON arg); no repo
literal or token in core (ambient gh). Hardened (#1130 follow-up): unknown labels are rejected with the valid
set (validate→reask, the model must use existing labels not invent them), and an optional `parent` links the
new issue as a native sub-issue via `gh issue edit --parent`.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=object))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402


class _R:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def _gh_dispatch(cap, *, labels="type/task\ntype/feature\nstatus/needs-decision\n",
                 create="https://github.com/owner/repo/issues/42\n", edit_rc=0):
    """A subprocess.run fake answering the gh calls create_issue makes: `gh label list`, `gh issue create`,
    `gh issue edit`. Records each cmd under cap['cmds']."""
    def run(cmd, **kw):
        cap.setdefault("cmds", []).append(cmd)
        if "label" in cmd and "list" in cmd:
            return _R(0, labels)
        if "issue" in cmd and "edit" in cmd:
            return _R(edit_rc, "" if edit_rc == 0 else "", "boom" if edit_rc else "")
        if "issue" in cmd and "create" in cmd:
            return _R(0, create)
        return _R(0, "")
    return run


def _present(monkeypatch):
    monkeypatch.setattr(gx10, "FORGE_ENABLED", True)
    monkeypatch.setattr(gx10.shutil, "which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr(gx10, "_is_sealed_profile", lambda: False)


# ── capability detection ──────────────────────────────────────────────────────
def test_create_issue_on_by_default_when_gh_present(monkeypatch):
    assert gx10.FORGE_ENABLED is True                                   # default flipped to ON
    monkeypatch.setattr(gx10.shutil, "which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr(gx10, "_is_sealed_profile", lambda: False)
    assert "create_issue" in {t["function"]["name"] for t in gx10._effective_tools()}


def test_create_issue_force_off_via_flag(monkeypatch):
    monkeypatch.setattr(gx10, "FORGE_ENABLED", False)
    monkeypatch.setattr(gx10.shutil, "which", lambda _: "/usr/bin/gh")
    assert "create_issue" not in {t["function"]["name"] for t in gx10._effective_tools()}
    out = gx10.run_tool("create_issue", {"title": "x", "body_file": "b.md"})
    assert out.startswith("ERROR: create_issue is force-disabled")


def test_create_issue_blocked_under_sealed(monkeypatch):
    monkeypatch.setattr(gx10, "FORGE_ENABLED", True)
    monkeypatch.setattr(gx10.shutil, "which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr(gx10, "_is_sealed_profile", lambda: True)
    assert "create_issue" not in {t["function"]["name"] for t in gx10._effective_tools()}
    out = gx10.run_tool("create_issue", {"title": "x", "body_file": "b.md"})
    assert out.startswith("ERROR: create_issue is blocked under the sealed")


def test_create_issue_needs_gh_present(monkeypatch, tmp_path):
    monkeypatch.setattr(gx10, "FORGE_ENABLED", True)
    monkeypatch.setattr(gx10.shutil, "which", lambda _: None)
    monkeypatch.setattr(gx10, "_is_sealed_profile", lambda: False)
    assert "create_issue" not in {t["function"]["name"] for t in gx10._effective_tools()}
    out = gx10.run_tool("create_issue", {"title": "x", "body_file": str(tmp_path / "b.md")})
    assert "gh" in out and out.startswith("ERROR")


# ── create + build ────────────────────────────────────────────────────────────
def test_create_issue_registered_and_builds_gh_command(monkeypatch, tmp_path):
    _present(monkeypatch)
    monkeypatch.setattr(gx10, "FORGE_REPO", "owner/repo")
    assert "create_issue" in {t["function"]["name"] for t in gx10._effective_tools()}
    bf = tmp_path / "body.md"
    bf.write_text("# Epic body\nlots of content\n", encoding="utf-8")
    cap = {}
    monkeypatch.setattr(gx10.subprocess, "run", _gh_dispatch(cap, labels="type/feature\nstatus/needs-decision\n"))
    out = gx10.run_tool("create_issue", {"title": "My Epic", "body_file": str(bf),
                                         "labels": "type/feature, status/needs-decision", "milestone": "M1"})
    assert "issues/42" in out
    c = [x for x in cap["cmds"] if "create" in x][0]                    # the `gh issue create` call
    assert c[:3] == ["gh", "issue", "create"]
    assert "--title" in c and "My Epic" in c
    assert "--body-file" in c and str(bf) in c                          # escape-free: body from the FILE
    assert "--repo" in c and "owner/repo" in c
    assert c.count("--label") == 2 and "type/feature" in c and "status/needs-decision" in c
    assert "--milestone" in c and "M1" in c


def test_create_issue_requires_an_existing_body_file(monkeypatch):
    _present(monkeypatch)
    out = gx10.run_tool("create_issue", {"title": "x", "body_file": "definitely_missing_body_9f3.md"})
    assert out.startswith("ERROR: body_file not found")                # steers the model to write the body first


# ── A) label validate → reask ─────────────────────────────────────────────────
def test_create_issue_rejects_unknown_label_with_the_valid_set(monkeypatch, tmp_path):
    # the model must use EXISTING labels, not invent them: an unknown label is rejected (issue NOT created)
    # with the valid set + a did-you-mean, so the model re-emits — not a silent drop, not a hard gh fail.
    _present(monkeypatch)
    bf = tmp_path / "b.md"; bf.write_text("body", encoding="utf-8")
    cap = {}
    monkeypatch.setattr(gx10.subprocess, "run", _gh_dispatch(cap, labels="type/task\ntype/feature\n"))
    out = gx10.run_tool("create_issue", {"title": "x", "body_file": str(bf),
                                         "labels": "type/task, area/model-evaluation"})
    assert out.startswith("ERROR: unknown label")
    assert "area/model-evaluation" in out                              # names the offender
    assert "type/task" in out and "type/feature" in out               # lists the valid set → the model can fix it
    assert not any("create" in c for c in cap["cmds"])                # and the issue was NOT created


# ── B) parent → native sub-issue link ─────────────────────────────────────────
def test_create_issue_links_parent_as_a_sub_issue(monkeypatch, tmp_path):
    _present(monkeypatch)
    monkeypatch.setattr(gx10, "FORGE_REPO", "owner/repo")
    bf = tmp_path / "b.md"; bf.write_text("body", encoding="utf-8")
    cap = {}
    monkeypatch.setattr(gx10.subprocess, "run", _gh_dispatch(cap))
    out = gx10.run_tool("create_issue", {"title": "sub", "body_file": str(bf), "parent": "#1136"})
    assert "linked it as a sub-issue of #1136" in out
    edit = [c for c in cap["cmds"] if "edit" in c][0]
    assert "--parent" in edit and "1136" in edit                       # leading '#' stripped
    assert "https://github.com/owner/repo/issues/42" in edit           # edits the newly-created issue by URL


def test_create_issue_parent_link_failure_is_reported_not_fatal(monkeypatch, tmp_path):
    _present(monkeypatch)
    bf = tmp_path / "b.md"; bf.write_text("body", encoding="utf-8")
    cap = {}
    monkeypatch.setattr(gx10.subprocess, "run", _gh_dispatch(cap, edit_rc=1))   # the link edit fails
    out = gx10.run_tool("create_issue", {"title": "sub", "body_file": str(bf), "parent": "999999"})
    assert out.startswith("OK: created issue")                         # the issue still exists (not raised away)
    assert "linking to parent #999999 failed" in out                   # but the link failure is surfaced


# ── fail-soft: a label-lookup hiccup never blocks a create ────────────────────
def test_create_issue_label_validation_is_failsoft(monkeypatch, tmp_path):
    _present(monkeypatch)
    bf = tmp_path / "b.md"; bf.write_text("body", encoding="utf-8")
    cap = {}

    def run(cmd, **kw):
        cap.setdefault("cmds", []).append(cmd)
        if "label" in cmd and "list" in cmd:
            return _R(1, "", "gh: label list unavailable")             # the vocabulary fetch FAILS
        if "issue" in cmd and "create" in cmd:
            return _R(0, "https://github.com/owner/repo/issues/7\n")
        return _R(0, "")
    monkeypatch.setattr(gx10.subprocess, "run", run)
    out = gx10.run_tool("create_issue", {"title": "x", "body_file": str(bf), "labels": "whatever/label"})
    assert "issues/7" in out                                          # created anyway — a hiccup never blocks
    create = [c for c in cap["cmds"] if "create" in c][0]
    assert "--label" in create and "whatever/label" in create         # label passed through unchanged
