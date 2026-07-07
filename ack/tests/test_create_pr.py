"""#1215 (epic #1212): the `create_pr` tool — open a PR through the forge adapter (open-only, does not merge).

The WRITE-sibling of create_issue on the forge seam (#1213): works on the `cli` (gh) and `native` (urllib)
transports, capability-detected + sealed-gated via _forge_available(), escape-free body-from-a-file.
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


# ── capability gating ─────────────────────────────────────────────────────────
def test_create_pr_offered_and_labelled_when_forge_available(monkeypatch):
    _present_cli(monkeypatch)
    assert "create_pr" in {t["function"]["name"] for t in gx10._effective_tools()}
    assert "create_pr" in gx10._all_tool_names() and "create_pr" in gx10._AUDIT_TOOLS


def test_create_pr_force_off_and_sealed(monkeypatch):
    monkeypatch.setattr(gx10, "FORGE_ENABLED", False)
    monkeypatch.setattr(gx10.shutil, "which", lambda _: "/usr/bin/gh")
    assert gx10.run_tool("create_pr", {"title": "x", "body_file": "b.md"}).startswith(
        "ERROR: create_pr is force-disabled")
    monkeypatch.setattr(gx10, "FORGE_ENABLED", True)
    monkeypatch.setattr(gx10, "_is_sealed_profile", lambda: True)
    assert gx10.run_tool("create_pr", {"title": "x", "body_file": "b.md"}).startswith(
        "ERROR: create_pr is blocked under the sealed")


def test_create_pr_requires_body_file(monkeypatch):
    _present_cli(monkeypatch)
    out = gx10.run_tool("create_pr", {"title": "x", "body_file": "definitely_missing_9f3.md"})
    assert out.startswith("ERROR: body_file not found")


def test_create_pr_empty_body_file_is_explicit_error(monkeypatch):
    # empty/omitted body_file must NOT resolve to the cwd dir (Path('.').exists() is True) and hand a
    # directory to the transport — it is an explicit "needs body_file" error.
    _present_cli(monkeypatch)
    called = {"n": 0}
    monkeypatch.setattr(gx10.subprocess, "run", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    for bad in ({"title": "x", "body_file": ""}, {"title": "x", "body_file": "   "}, {"title": "x"}):
        assert gx10.run_tool("create_pr", bad).startswith("ERROR: create_pr needs body_file")
    assert called["n"] == 0                                # never shelled out with a directory


def test_create_pr_is_server_side_not_ingestion_or_local(monkeypatch):
    # open-only + server-side guarantee: a PR-open is a mutating outbound write (audited), returns its own
    # URL (not ingested external content), and runs server-side (not client-bridged).
    assert "create_pr" in gx10._AUDIT_TOOLS
    assert "create_pr" not in gx10._INGESTION_TOOLS
    assert "create_pr" not in gx10.LOCAL_TOOL_NAMES


def test_native_create_pr_bad_repo_is_error_not_false_success(tmp_path):
    def h(method, url, req):
        raise _http_error(422, "Validation Failed")
    bf = tmp_path / "b.md"; bf.write_text("b", encoding="utf-8")
    st, msg = NativeForgeAdapter("tok", "o/r", opener=_opener(h)).create_pr("T", bf, "main", "feat/x", False)
    assert st == "error" and "422" in msg


# ── cli argv ──────────────────────────────────────────────────────────────────
def test_create_pr_cli_builds_gh_command(monkeypatch, tmp_path):
    _present_cli(monkeypatch)
    bf = tmp_path / "body.md"; bf.write_text("Closes #1215\nbody", encoding="utf-8")
    cap = {}

    def run(cmd, **kw):
        cap["cmd"] = cmd
        return _R(0, "https://github.com/owner/repo/pull/77\n")
    monkeypatch.setattr(gx10.subprocess, "run", run)
    out = gx10.run_tool("create_pr", {"title": "My PR", "body_file": str(bf),
                                      "base": "main", "head": "feat/x", "draft": "true"})
    c = cap["cmd"]
    assert c[:3] == ["gh", "pr", "create"]
    assert "--title" in c and "My PR" in c
    assert "--body-file" in c and str(bf) in c
    assert "--repo" in c and "owner/repo" in c
    assert "--base" in c and "main" in c and "--head" in c and "feat/x" in c and "--draft" in c
    assert "pull/77" in out and out.startswith("OK: opened PR")


def test_create_pr_cli_surfaces_push_error_verbatim(monkeypatch, tmp_path):
    _present_cli(monkeypatch)
    bf = tmp_path / "b.md"; bf.write_text("b", encoding="utf-8")
    monkeypatch.setattr(gx10.subprocess, "run",
                        lambda *a, **k: _R(1, "", "must first push the current branch to a remote"))
    out = gx10.run_tool("create_pr", {"title": "x", "body_file": str(bf)})
    assert out.startswith("ERROR: gh pr create failed") and "must first push" in out


# ── native ────────────────────────────────────────────────────────────────────
def test_native_create_pr_posts_pull_with_defaulted_base(tmp_path):
    seen = {}

    def h(method, url, req):
        if method == "GET" and url.endswith("/repos/o/r"):
            return {"default_branch": "main"}
        if method == "POST" and url.endswith("/pulls"):
            seen["body"] = json.loads(req.data.decode("utf-8"))
            return {"html_url": "https://github.com/o/r/pull/9"}
        return {}
    bf = tmp_path / "b.md"; bf.write_text("pr body", encoding="utf-8")
    st, url = NativeForgeAdapter("tok", "o/r", opener=_opener(h)).create_pr("T", bf, None, "feat/x", True)
    assert st == "ok" and url.endswith("/pull/9")
    assert seen["body"] == {"title": "T", "head": "feat/x", "base": "main", "body": "pr body", "draft": True}


def test_native_create_pr_requires_head(tmp_path):
    bf = tmp_path / "b.md"; bf.write_text("b", encoding="utf-8")
    st, msg = NativeForgeAdapter("tok", "o/r", opener=_opener(lambda *a: {})).create_pr("T", bf, "main", None, False)
    assert st == "error" and "head" in msg


def test_create_pr_native_gate_names_token(monkeypatch):
    monkeypatch.setattr(gx10, "FORGE_ENABLED", True)
    monkeypatch.setattr(gx10, "FORGE_ADAPTER", "native")
    monkeypatch.setattr(gx10, "FORGE_REPO", "o/r")
    monkeypatch.setattr(gx10, "_is_sealed_profile", lambda: False)
    monkeypatch.delenv("GX10_FORGE_TOKEN", raising=False)
    assert "create_pr" not in {t["function"]["name"] for t in gx10._effective_tools()}
    assert gx10.run_tool("create_pr", {"title": "x", "body_file": "b.md"}).startswith(
        "ERROR: create_pr (native forge) needs a token")


# ── the keystone-on-native proof: open a PR with NO gh on PATH ────────────────
def test_create_pr_works_on_native_without_gh(monkeypatch, tmp_path):
    monkeypatch.setattr(gx10, "FORGE_ENABLED", True)
    monkeypatch.setattr(gx10, "FORGE_ADAPTER", "native")
    monkeypatch.setattr(gx10, "FORGE_REPO", "o/r")
    monkeypatch.setattr(gx10, "FORGE_TOKEN_ENV", "GX10_FORGE_TOKEN")
    monkeypatch.setenv("GX10_FORGE_TOKEN", "tok")
    monkeypatch.setattr(gx10, "_is_sealed_profile", lambda: False)
    monkeypatch.setattr(gx10.shutil, "which", lambda _: None)   # NO gh
    bf = tmp_path / "b.md"; bf.write_text("Closes #1215", encoding="utf-8")

    def h(method, url, req):
        if method == "POST" and url.endswith("/pulls"):
            return {"html_url": "https://github.com/o/r/pull/12"}
        return {"default_branch": "main"}
    monkeypatch.setattr(urllib.request, "urlopen", _opener(h))
    out = gx10.run_tool("create_pr", {"title": "T", "body_file": str(bf), "head": "feat/x"})
    assert out.startswith("OK: opened PR") and "pull/12" in out
