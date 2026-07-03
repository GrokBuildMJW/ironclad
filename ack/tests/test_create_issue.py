"""#1073 (epic #1043 quick-win): the gated, secret-free, escape-free `create_issue` tool.

Lets the orchestrator FILE its own tracker issues (GitHub via the `gh` CLI) instead of falling back to
writing a body file it cannot submit. Default OFF (opt-in `forge.enabled`); the issue body comes from a
FILE (no giant JSON arg); no repo literal or token in core (ambient gh auth).
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


def test_create_issue_gated_off_by_default(monkeypatch):
    monkeypatch.setattr(gx10, "FORGE_ENABLED", False)
    names = {t["function"]["name"] for t in gx10._effective_tools()}
    assert "create_issue" not in names                                  # not registered when off (byte-identical)
    out = gx10.run_tool("create_issue", {"title": "x", "body_file": "b.md"})
    assert out.startswith("ERROR: create_issue is disabled")            # and the executor refuses too (double-gate)


def test_create_issue_registered_and_builds_gh_command(monkeypatch, tmp_path):
    monkeypatch.setattr(gx10, "FORGE_ENABLED", True)
    monkeypatch.setattr(gx10, "FORGE_REPO", "owner/repo")
    assert "create_issue" in {t["function"]["name"] for t in gx10._effective_tools()}
    monkeypatch.setattr(gx10.shutil, "which", lambda _: "/usr/bin/gh")
    bf = tmp_path / "body.md"
    bf.write_text("# Epic body\nlots of content\n", encoding="utf-8")
    cap = {}

    class _R:
        returncode = 0
        stdout = "https://github.com/owner/repo/issues/42\n"
        stderr = ""

    monkeypatch.setattr(gx10.subprocess, "run", lambda cmd, **kw: (cap.__setitem__("cmd", cmd) or _R()))
    out = gx10.run_tool("create_issue", {"title": "My Epic", "body_file": str(bf),
                                         "labels": "type/feature, status/needs-decision", "milestone": "M1"})
    assert "issues/42" in out
    c = cap["cmd"]
    assert c[:3] == ["gh", "issue", "create"]
    assert "--title" in c and "My Epic" in c
    assert "--body-file" in c and str(bf) in c                          # escape-free: body from the FILE
    assert "--repo" in c and "owner/repo" in c
    assert c.count("--label") == 2 and "type/feature" in c and "status/needs-decision" in c
    assert "--milestone" in c and "M1" in c


def test_create_issue_requires_an_existing_body_file(monkeypatch):
    monkeypatch.setattr(gx10, "FORGE_ENABLED", True)
    monkeypatch.setattr(gx10.shutil, "which", lambda _: "/usr/bin/gh")
    out = gx10.run_tool("create_issue", {"title": "x", "body_file": "definitely_missing_body_9f3.md"})
    assert out.startswith("ERROR: body_file not found")                # steers the model to write the body first


def test_create_issue_needs_gh_present(monkeypatch, tmp_path):
    monkeypatch.setattr(gx10, "FORGE_ENABLED", True)
    monkeypatch.setattr(gx10.shutil, "which", lambda _: None)          # gh not installed / not on PATH
    out = gx10.run_tool("create_issue", {"title": "x", "body_file": str(tmp_path / "b.md")})
    assert "gh" in out and out.startswith("ERROR")
