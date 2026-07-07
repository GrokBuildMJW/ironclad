"""#1219 (epic #1212): the `pr_status` tool — read a PR's CI/mergeability SNAPSHOT (non-blocking).

Collapses the audit's pr_checks_status + view_pr into one merge-readiness read on the forge seam (#1213).
The load-bearing subtlety: `gh pr checks` EXITS NON-ZERO as DATA (pending=8, fail=1) — the cli adapter must
parse stdout, not treat the exit code as an error.
"""
from __future__ import annotations

import io
import json
import sys
import types
import urllib.error
import urllib.request
from pathlib import Path

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=object))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402
from forge_native import NativeForgeAdapter  # noqa: E402


class _R:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class _Resp:
    def __init__(self, obj):
        self._b = json.dumps(obj).encode("utf-8")

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _opener(handler):
    def _open(req, timeout=None):
        return _Resp(handler(req.get_method(), req.full_url, req))
    return _open


def _http_error(code, msg="err"):
    return urllib.error.HTTPError("http://x", code, msg, {}, io.BytesIO(json.dumps({"message": msg}).encode()))


def _present_cli(monkeypatch):
    monkeypatch.setattr(gx10, "FORGE_ENABLED", True)
    monkeypatch.setattr(gx10, "FORGE_ADAPTER", "cli")
    monkeypatch.setattr(gx10, "FORGE_REPO", "owner/repo")
    monkeypatch.setattr(gx10.shutil, "which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr(gx10, "_is_sealed_profile", lambda: False)


_VIEW = json.dumps({"state": "OPEN", "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
                    "reviewDecision": "APPROVED"})


def _cli_dispatch(view=_VIEW, view_rc=0, checks="[]", checks_rc=0):
    def run(cmd, **kw):
        if "pr" in cmd and "view" in cmd:
            return _R(view_rc, view if view_rc == 0 else "", "" if view_rc == 0 else "gh: could not resolve to a PullRequest with the number of 999.")
        if "pr" in cmd and "checks" in cmd:
            return _R(checks_rc, checks)   # exit code may be non-zero WITH a JSON payload
        return _R(0, "")
    return run


# ── gating + registration ─────────────────────────────────────────────────────
def test_pr_status_offered_and_registered_as_a_read(monkeypatch):
    _present_cli(monkeypatch)
    assert "pr_status" in {t["function"]["name"] for t in gx10._effective_tools()}
    assert "pr_status" in gx10._all_tool_names()
    assert "pr_status" in gx10._INGESTION_TOOLS            # a READ of external content → injection-fenced
    assert "pr_status" not in gx10._AUDIT_TOOLS            # not a mutation
    assert "pr_status" not in gx10.LOCAL_TOOL_NAMES        # server-side


def test_pr_status_native_gate_names_token(monkeypatch):
    monkeypatch.setattr(gx10, "FORGE_ENABLED", True)
    monkeypatch.setattr(gx10, "FORGE_ADAPTER", "native")
    monkeypatch.setattr(gx10, "FORGE_REPO", "o/r")
    monkeypatch.setattr(gx10, "_is_sealed_profile", lambda: False)
    monkeypatch.delenv("GX10_FORGE_TOKEN", raising=False)
    assert "pr_status" not in {t["function"]["name"] for t in gx10._effective_tools()}
    assert gx10.run_tool("pr_status", {"number": "1"}).startswith("ERROR: pr_status (native forge) needs a token")


def test_pr_status_non_numeric_guarded_before_shell(monkeypatch):
    _present_cli(monkeypatch)
    called = {"n": 0}
    monkeypatch.setattr(gx10.subprocess, "run", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    assert gx10.run_tool("pr_status", {"number": "abc"}).startswith("ERROR: pr_status needs a numeric PR number")
    assert called["n"] == 0


# ── the GOTCHA: gh pr checks non-zero exit is DATA, not an error ──────────────
def test_pr_status_cli_pending_exit8_parsed_as_data(monkeypatch):
    _present_cli(monkeypatch)
    checks = json.dumps([{"name": "tests", "bucket": "pass"}, {"name": "deploy", "bucket": "pending"}])
    monkeypatch.setattr(gx10.subprocess, "run", _cli_dispatch(checks=checks, checks_rc=8))   # exit 8 = pending
    out = gx10.run_tool("pr_status", {"number": "#1216"})
    assert "1 PENDING" in out and "PENDING   deploy" in out and "PASS      tests" in out
    assert "mergeable: MERGEABLE" in out and "review: APPROVED" in out


def test_pr_status_cli_failing_exit1_parsed_as_data(monkeypatch):
    _present_cli(monkeypatch)
    checks = json.dumps([{"name": "tests", "bucket": "fail"}, {"name": "lint", "bucket": "pass"}])
    monkeypatch.setattr(gx10.subprocess, "run", _cli_dispatch(checks=checks, checks_rc=1))   # exit 1 = failing
    out = gx10.run_tool("pr_status", {"number": "5"})
    assert "1 FAILING" in out and "FAIL      tests" in out


def test_pr_status_cli_all_passing(monkeypatch):
    _present_cli(monkeypatch)
    checks = json.dumps([{"name": "a", "bucket": "pass"}, {"name": "b", "bucket": "pass"}])
    monkeypatch.setattr(gx10.subprocess, "run", _cli_dispatch(checks=checks, checks_rc=0))
    assert "ALL PASSING (2 checks)" in gx10.run_tool("pr_status", {"number": "5"})


def test_pr_status_cli_no_checks(monkeypatch):
    _present_cli(monkeypatch)
    monkeypatch.setattr(gx10.subprocess, "run", _cli_dispatch(checks="", checks_rc=0))   # empty stdout
    assert "no checks reported" in gx10.run_tool("pr_status", {"number": "5"})


def test_pr_status_cli_not_found(monkeypatch):
    _present_cli(monkeypatch)
    monkeypatch.setattr(gx10.subprocess, "run", _cli_dispatch(view_rc=1))   # gh pr view can't resolve the PR
    out = gx10.run_tool("pr_status", {"number": "999"})
    assert out.startswith("NOT_FOUND: PR #999 does not exist") and "authoritative" in out


# ── native ────────────────────────────────────────────────────────────────────
def test_native_pr_status_maps_checkruns():
    def h(method, url, req):
        if url.endswith("/pulls/9"):
            return {"state": "open", "mergeable": True, "mergeable_state": "clean", "merged": False,
                    "head": {"sha": "abc"}}
        if "/commits/abc/check-runs" in url:
            return {"check_runs": [{"name": "ci", "status": "completed", "conclusion": "success"},
                                   {"name": "lint", "status": "in_progress"}]}
        return {}
    st, data = NativeForgeAdapter("tok", "o/r", opener=_opener(h)).pr_status(9)
    assert st == "ok"
    assert data["mergeable"] == "MERGEABLE" and data["mergeStateStatus"] == "CLEAN"
    buckets = {c["name"]: c["bucket"] for c in data["checks"]}
    assert buckets == {"ci": "pass", "lint": "pending"}


def test_native_pr_status_repo_404_is_error(tmp_path):
    st, msg = NativeForgeAdapter("tok", "o/typo", opener=_opener(lambda *a: (_ for _ in ()).throw(_http_error(404)))).pr_status(5)
    assert st == "error" and "repository" in msg.lower()


def test_native_pr_status_missing_pr_is_not_found():
    def h(method, url, req):
        if url.endswith("/pulls/999"):
            raise _http_error(404, "Not Found")
        return {"full_name": "o/r"}   # repo exists
    st, _ = NativeForgeAdapter("tok", "o/r", opener=_opener(h)).pr_status(999)
    assert st == "not_found"


def test_native_pr_status_includes_legacy_commit_statuses():
    # gh pr checks aggregates check-runs AND commit statuses; native must too (external CI via the Status API)
    def h(method, url, req):
        if url.endswith("/pulls/9"):
            return {"state": "open", "mergeable": True, "mergeable_state": "unstable", "head": {"sha": "abc"}}
        if "/commits/abc/check-runs" in url:
            return {"check_runs": [{"name": "ci", "status": "completed", "conclusion": "success"}]}
        if "/commits/abc/status" in url:
            return {"statuses": [{"context": "jenkins", "state": "failure"}]}
        return {}
    st, data = NativeForgeAdapter("tok", "o/r", opener=_opener(h)).pr_status(9)
    assert st == "ok"
    assert {c["name"]: c["bucket"] for c in data["checks"]} == {"ci": "pass", "jenkins": "fail"}


def test_native_pr_status_paginates_check_runs():
    # a failing check-run on page 2 must not be missed (else a false ALL PASSING on a >100-check PR)
    def h(method, url, req):
        if url.endswith("/pulls/9"):
            return {"state": "open", "mergeable": True, "mergeable_state": "clean", "head": {"sha": "abc"}}
        if "/check-runs" in url:
            if "&page=1" in url:
                return {"check_runs": [{"name": f"c{i}", "status": "completed", "conclusion": "success"}
                                       for i in range(100)]}
            return {"check_runs": [{"name": "late", "status": "completed", "conclusion": "failure"}]}
        return {}
    st, data = NativeForgeAdapter("tok", "o/r", opener=_opener(h)).pr_status(9)
    assert st == "ok" and len(data["checks"]) == 101
    assert {c["name"]: c["bucket"] for c in data["checks"]}.get("late") == "fail"


def test_native_pr_status_reviewdecision_from_reviews():
    # native has no GraphQL reviewDecision — approximate from /reviews: outstanding CHANGES_REQUESTED wins
    def h(method, url, req):
        if url.endswith("/pulls/9"):
            return {"state": "open", "mergeable": True, "mergeable_state": "clean", "head": {"sha": "abc"}}
        if "/pulls/9/reviews" in url:
            return [{"user": {"login": "alice"}, "state": "APPROVED"},
                    {"user": {"login": "bob"}, "state": "CHANGES_REQUESTED"}]
        return {}
    st, data = NativeForgeAdapter("tok", "o/r", opener=_opener(h)).pr_status(9)
    assert st == "ok" and data["reviewDecision"] == "CHANGES_REQUESTED"


def test_pr_status_cli_repo_error_is_error_not_not_found(monkeypatch):
    # a misconfigured forge.repo makes `gh pr view` say "could not resolve to a Repository" → real ERROR
    _present_cli(monkeypatch)

    def run(cmd, **kw):
        if "pr" in cmd and "view" in cmd:
            return _R(1, "", "GraphQL: Could not resolve to a Repository with the name 'owner/typo'.")
        return _R(0, "")
    monkeypatch.setattr(gx10.subprocess, "run", run)
    out = gx10.run_tool("pr_status", {"number": "5"})
    assert out.startswith("ERROR: gh pr view failed") and not out.startswith("NOT_FOUND")


def test_pr_status_works_on_native_without_gh(monkeypatch):
    monkeypatch.setattr(gx10, "FORGE_ENABLED", True)
    monkeypatch.setattr(gx10, "FORGE_ADAPTER", "native")
    monkeypatch.setattr(gx10, "FORGE_REPO", "o/r")
    monkeypatch.setattr(gx10, "FORGE_TOKEN_ENV", "GX10_FORGE_TOKEN")
    monkeypatch.setenv("GX10_FORGE_TOKEN", "tok")
    monkeypatch.setattr(gx10, "_is_sealed_profile", lambda: False)
    monkeypatch.setattr(gx10.shutil, "which", lambda _: None)   # NO gh

    def h(method, url, req):
        if "/pulls/3" in url:
            return {"state": "open", "mergeable": True, "mergeable_state": "clean", "head": {"sha": "z"}}
        if "/check-runs" in url:
            return {"check_runs": [{"name": "ci", "status": "completed", "conclusion": "success"}]}
        return {}
    monkeypatch.setattr(urllib.request, "urlopen", _opener(h))
    out = gx10.run_tool("pr_status", {"number": "3"})
    assert "ALL PASSING (1 checks)" in out and "mergeable: MERGEABLE" in out
