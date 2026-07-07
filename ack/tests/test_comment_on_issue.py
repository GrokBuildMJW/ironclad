"""#1217 (epic #1212): the `comment_on_issue` tool — append a comment through the forge adapter (narrow).

The third leg of create/read/comment on the forge seam (#1213): cli (gh) + native (urllib) + mock,
capability-detected + sealed-gated, escape-free body-from-a-file, authoritative NOT_FOUND for a missing
issue. Comment-ONLY — no close/relabel.
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


# ── gating + registration ─────────────────────────────────────────────────────
def test_comment_offered_and_registered(monkeypatch):
    _present_cli(monkeypatch)
    assert "comment_on_issue" in {t["function"]["name"] for t in gx10._effective_tools()}
    assert "comment_on_issue" in gx10._all_tool_names() and "comment_on_issue" in gx10._AUDIT_TOOLS
    assert "comment_on_issue" not in gx10._INGESTION_TOOLS and "comment_on_issue" not in gx10.LOCAL_TOOL_NAMES


def test_comment_force_off_and_sealed(monkeypatch):
    monkeypatch.setattr(gx10, "FORGE_ENABLED", False)
    monkeypatch.setattr(gx10.shutil, "which", lambda _: "/usr/bin/gh")
    assert gx10.run_tool("comment_on_issue", {"number": "1", "body_file": "b.md"}).startswith(
        "ERROR: comment_on_issue is force-disabled")
    monkeypatch.setattr(gx10, "FORGE_ENABLED", True)
    monkeypatch.setattr(gx10, "_is_sealed_profile", lambda: True)
    assert gx10.run_tool("comment_on_issue", {"number": "1", "body_file": "b.md"}).startswith(
        "ERROR: comment_on_issue is blocked under the sealed")


def test_comment_native_gate_names_token(monkeypatch):
    monkeypatch.setattr(gx10, "FORGE_ENABLED", True)
    monkeypatch.setattr(gx10, "FORGE_ADAPTER", "native")
    monkeypatch.setattr(gx10, "FORGE_REPO", "o/r")
    monkeypatch.setattr(gx10, "_is_sealed_profile", lambda: False)
    monkeypatch.delenv("GX10_FORGE_TOKEN", raising=False)
    assert "comment_on_issue" not in {t["function"]["name"] for t in gx10._effective_tools()}
    assert gx10.run_tool("comment_on_issue", {"number": "1", "body_file": "b.md"}).startswith(
        "ERROR: comment_on_issue (native forge) needs a token")


# ── input validation (before any transport) ───────────────────────────────────
def test_comment_non_numeric_number_guarded_before_shell(monkeypatch):
    _present_cli(monkeypatch)
    called = {"n": 0}
    monkeypatch.setattr(gx10.subprocess, "run", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    assert gx10.run_tool("comment_on_issue", {"number": "abc", "body_file": "b.md"}).startswith(
        "ERROR: comment_on_issue needs a numeric issue number")
    assert called["n"] == 0


def test_comment_body_file_missing_and_empty(monkeypatch):
    _present_cli(monkeypatch)
    called = {"n": 0}
    monkeypatch.setattr(gx10.subprocess, "run", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    assert gx10.run_tool("comment_on_issue", {"number": "5", "body_file": ""}).startswith(
        "ERROR: comment_on_issue needs body_file")
    assert gx10.run_tool("comment_on_issue", {"number": "5"}).startswith(
        "ERROR: comment_on_issue needs body_file")
    assert gx10.run_tool("comment_on_issue", {"number": "5", "body_file": "missing_9f3.md"}).startswith(
        "ERROR: body_file not found")
    assert called["n"] == 0


# ── cli ───────────────────────────────────────────────────────────────────────
def test_comment_cli_builds_gh_command_and_strips_hash(monkeypatch, tmp_path):
    _present_cli(monkeypatch)
    bf = tmp_path / "c.md"; bf.write_text("a datapoint", encoding="utf-8")
    cap = {}

    def run(cmd, **kw):
        cap["cmd"] = cmd
        return _R(0, "https://github.com/owner/repo/issues/1211#issuecomment-99\n")
    monkeypatch.setattr(gx10.subprocess, "run", run)
    out = gx10.run_tool("comment_on_issue", {"number": "#1211", "body_file": str(bf)})
    c = cap["cmd"]
    assert c[:3] == ["gh", "issue", "comment"] and c[3] == "1211"          # '#' stripped
    assert "--body-file" in c and str(bf) in c and "--repo" in c and "owner/repo" in c
    assert out.startswith("OK: commented on #1211:") and "issuecomment-99" in out


def test_comment_cli_missing_issue_is_not_found(monkeypatch, tmp_path):
    _present_cli(monkeypatch)
    bf = tmp_path / "c.md"; bf.write_text("x", encoding="utf-8")
    monkeypatch.setattr(gx10.subprocess, "run",
                        lambda *a, **k: _R(1, "", "GraphQL: Could not resolve to an Issue with the number of 999999."))
    out = gx10.run_tool("comment_on_issue", {"number": "999999", "body_file": str(bf)})
    assert out.startswith("NOT_FOUND: issue #999999 does not exist") and "authoritative" in out


# ── native ────────────────────────────────────────────────────────────────────
def test_native_comment_posts_body(tmp_path):
    seen = {}

    def h(method, url, req):
        if method == "POST" and url.endswith("/issues/7/comments"):
            seen["body"] = json.loads(req.data.decode("utf-8"))
            return {"html_url": "https://github.com/o/r/issues/7#issuecomment-5"}
        return {}
    bf = tmp_path / "c.md"; bf.write_text("hello", encoding="utf-8")
    st, url = NativeForgeAdapter("tok", "o/r", opener=_opener(h)).comment_on_issue(7, bf)
    assert st == "ok" and url.endswith("#issuecomment-5") and seen["body"] == {"body": "hello"}


def test_native_comment_repo_404_is_error_not_false_not_found(tmp_path):
    def h(method, url, req):
        raise _http_error(404, "Not Found")
    bf = tmp_path / "c.md"; bf.write_text("x", encoding="utf-8")
    st, msg = NativeForgeAdapter("tok", "o/typo", opener=_opener(h)).comment_on_issue(5, bf)
    assert st == "error" and "repository" in msg.lower()


def test_native_comment_missing_issue_is_authoritative_not_found(tmp_path):
    # the ISSUE POST 404s but the repo-root probe succeeds → authoritative not_found (the native #1208 guard)
    def h(method, url, req):
        if "/comments" in url:
            raise _http_error(404, "Not Found")
        return {"full_name": "o/r"}
    bf = tmp_path / "c.md"; bf.write_text("x", encoding="utf-8")
    st, _ = NativeForgeAdapter("tok", "o/r", opener=_opener(h)).comment_on_issue(999999, bf)
    assert st == "not_found"


def test_comment_works_on_native_without_gh(monkeypatch, tmp_path):
    monkeypatch.setattr(gx10, "FORGE_ENABLED", True)
    monkeypatch.setattr(gx10, "FORGE_ADAPTER", "native")
    monkeypatch.setattr(gx10, "FORGE_REPO", "o/r")
    monkeypatch.setattr(gx10, "FORGE_TOKEN_ENV", "GX10_FORGE_TOKEN")
    monkeypatch.setenv("GX10_FORGE_TOKEN", "tok")
    monkeypatch.setattr(gx10, "_is_sealed_profile", lambda: False)
    monkeypatch.setattr(gx10.shutil, "which", lambda _: None)   # NO gh
    bf = tmp_path / "c.md"; bf.write_text("note", encoding="utf-8")
    monkeypatch.setattr(urllib.request, "urlopen",
                        _opener(lambda m, u, r: {"html_url": "https://github.com/o/r/issues/3#issuecomment-2"}))
    out = gx10.run_tool("comment_on_issue", {"number": "3", "body_file": str(bf)})
    assert out.startswith("OK: commented on #3:") and "issuecomment-2" in out
