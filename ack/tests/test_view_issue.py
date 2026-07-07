"""#1208 (epic #1043): the `view_issue` tool — the READ counterpart to `create_issue`, CAPABILITY-DETECTED.

Gives the orchestrator the first-class path for resolving a `#NNN` reference: query the tracker directly
(GitHub via the `gh` CLI) instead of grepping git history (which only cites issues a merged PR closed, so an
open issue is invisible there → the agent that flailed on "check #1207" and falsely concluded "does not
exist"). Offered together with `create_issue` under the same `_forge_available()` gate (gh present, not
sealed, `forge.enabled`); a non-existent number returns an authoritative `NOT_FOUND` (the tracker WAS
queried), never an inference from a missing commit.
"""
from __future__ import annotations

import json
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


def _issue_json(number=1207, state="OPEN", title="flaky watchdog test",
                labels=("type/bug", "area/ci"), milestone="Phase X", url=None, body="the body"):
    return json.dumps({
        "number": number, "state": state, "title": title,
        "labels": [{"name": n} for n in labels],
        "milestone": ({"title": milestone} if milestone else None),
        "url": url or f"https://github.com/owner/repo/issues/{number}",
        "body": body,
    })


def _present(monkeypatch):
    monkeypatch.setattr(gx10, "FORGE_ENABLED", True)
    monkeypatch.setattr(gx10.shutil, "which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr(gx10, "_is_sealed_profile", lambda: False)


# ── capability detection (mirrors create_issue) ───────────────────────────────
def test_view_issue_offered_by_default_when_gh_present(monkeypatch):
    _present(monkeypatch)
    names = {t["function"]["name"] for t in gx10._effective_tools()}
    assert "view_issue" in names and "create_issue" in names            # the forge surface is offered together


def test_view_issue_force_off_via_flag(monkeypatch):
    monkeypatch.setattr(gx10, "FORGE_ENABLED", False)
    monkeypatch.setattr(gx10.shutil, "which", lambda _: "/usr/bin/gh")
    assert "view_issue" not in {t["function"]["name"] for t in gx10._effective_tools()}
    out = gx10.run_tool("view_issue", {"number": "1207"})
    assert out.startswith("ERROR: view_issue is force-disabled")


def test_view_issue_blocked_under_sealed(monkeypatch):
    monkeypatch.setattr(gx10, "FORGE_ENABLED", True)
    monkeypatch.setattr(gx10.shutil, "which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr(gx10, "_is_sealed_profile", lambda: True)
    assert "view_issue" not in {t["function"]["name"] for t in gx10._effective_tools()}
    out = gx10.run_tool("view_issue", {"number": "1207"})
    assert out.startswith("ERROR: view_issue is blocked under the sealed")


def test_view_issue_needs_gh_present(monkeypatch):
    monkeypatch.setattr(gx10, "FORGE_ENABLED", True)
    monkeypatch.setattr(gx10.shutil, "which", lambda _: None)
    monkeypatch.setattr(gx10, "_is_sealed_profile", lambda: False)
    assert "view_issue" not in {t["function"]["name"] for t in gx10._effective_tools()}
    out = gx10.run_tool("view_issue", {"number": "1207"})
    assert "gh" in out and out.startswith("ERROR")


# ── read + build ──────────────────────────────────────────────────────────────
def test_view_issue_builds_gh_command_and_renders(monkeypatch):
    _present(monkeypatch)
    monkeypatch.setattr(gx10, "FORGE_REPO", "owner/repo")
    cap = {}

    def run(cmd, **kw):
        cap["cmd"] = cmd
        return _R(0, _issue_json(number=1207, state="OPEN", title="flaky watchdog test",
                                 labels=("type/bug", "area/ci"), milestone="Phase X", body="the body"))
    monkeypatch.setattr(gx10.subprocess, "run", run)
    out = gx10.run_tool("view_issue", {"number": "1207"})
    c = cap["cmd"]
    assert c[:4] == ["gh", "issue", "view", "1207"]                     # the `gh issue view <N>` call
    assert "--json" in c and "--repo" in c and "owner/repo" in c        # structured + repo-scoped
    assert "#1207" in out and "[OPEN]" in out and "flaky watchdog test" in out
    assert "type/bug" in out and "area/ci" in out                       # labels rendered
    assert "https://github.com/owner/repo/issues/1207" in out           # url rendered
    assert "the body" in out                                            # body rendered


def test_view_issue_strips_leading_hash(monkeypatch):
    _present(monkeypatch)
    cap = {}

    def run(cmd, **kw):
        cap["cmd"] = cmd
        return _R(0, _issue_json(number=42))
    monkeypatch.setattr(gx10.subprocess, "run", run)
    gx10.run_tool("view_issue", {"number": "#42"})
    assert cap["cmd"][3] == "42"                                        # leading '#' stripped for gh


def test_view_issue_rejects_non_numeric(monkeypatch):
    _present(monkeypatch)
    called = {"n": 0}
    monkeypatch.setattr(gx10.subprocess, "run", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    out = gx10.run_tool("view_issue", {"number": "abc"})
    assert out.startswith("ERROR: view_issue needs a numeric issue number")
    assert called["n"] == 0                                             # guarded BEFORE shelling out to gh


def test_view_issue_missing_is_authoritative_not_found(monkeypatch):
    # the prüfstein: a non-existent number returns an AUTHORITATIVE not-found (the tracker WAS queried) —
    # never an inference from a missing commit, so the model does not fall back to git-history grepping.
    _present(monkeypatch)
    monkeypatch.setattr(gx10, "FORGE_REPO", "owner/repo")
    monkeypatch.setattr(gx10.subprocess, "run",
                        lambda *a, **k: _R(1, "", "GraphQL: Could not resolve to an Issue with the number of 999999."))
    out = gx10.run_tool("view_issue", {"number": "999999"})
    assert out.startswith("NOT_FOUND: issue #999999 does not exist")
    assert "owner/repo" in out and "authoritative" in out


def test_view_issue_gh_error_is_surfaced_not_raised(monkeypatch):
    _present(monkeypatch)
    monkeypatch.setattr(gx10.subprocess, "run", lambda *a, **k: _R(1, "", "gh: server error 500"))
    out = gx10.run_tool("view_issue", {"number": "5"})
    assert out.startswith("ERROR: gh issue view failed") and "500" in out


def test_view_issue_body_is_bounded(monkeypatch):
    _present(monkeypatch)
    monkeypatch.setattr(gx10.subprocess, "run",
                        lambda *a, **k: _R(0, _issue_json(number=1, body="x" * 9000)))
    out = gx10.run_tool("view_issue", {"number": "1"})
    assert "[body truncated]" in out and len(out) < 6000                # a huge body cannot blow the window


def test_view_issue_renders_null_fields(monkeypatch):
    # labels/milestone/body may be null in the gh payload — rendering must not crash (null-safe).
    _present(monkeypatch)
    monkeypatch.setattr(gx10.subprocess, "run", lambda *a, **k: _R(0, json.dumps(
        {"number": 3, "state": "CLOSED", "title": "t", "labels": None,
         "milestone": None, "url": "https://github.com/o/r/issues/3", "body": None})))
    out = gx10.run_tool("view_issue", {"number": "3"})
    assert "#3" in out and "[CLOSED]" in out and "labels: -" in out and "milestone: -" in out


def test_view_issue_repo_resolution_error_is_not_a_false_not_found(monkeypatch):
    # a MISCONFIGURED forge.repo also makes gh say "could not resolve" — that must be a real ERROR, not an
    # authoritative "issue does not exist" (which would undermine the whole NOT_FOUND promise).
    _present(monkeypatch)
    monkeypatch.setattr(gx10, "FORGE_REPO", "owner/does-not-exist")
    monkeypatch.setattr(gx10.subprocess, "run", lambda *a, **k: _R(
        1, "", "GraphQL: Could not resolve to a Repository with the name 'owner/does-not-exist'."))
    out = gx10.run_tool("view_issue", {"number": "5"})
    assert out.startswith("ERROR: gh issue view failed")               # repo error surfaced as an ERROR
    assert not out.startswith("NOT_FOUND")                             # NOT a false "issue does not exist"


def test_view_issue_is_in_all_tool_names(monkeypatch):
    # S12 shell-redirect + the #503 plugin-collision guard both key off _all_tool_names(); the tool must be
    # present there or a `view_issue …` typed into the shell is not redirected and a plugin can shadow it.
    assert "view_issue" in gx10._all_tool_names()
