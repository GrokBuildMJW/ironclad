"""#1213 (epic #1212): the forge adapter seam — `cli` (gh) | `native` (stdlib urllib) | `mock`.

Proves the forge tools are GENERAL in ironclad, not `gh`-on-the-box: the native adapter reads/writes the
GitHub REST API over stdlib urllib with a token, so `create_issue`/`view_issue` work with **no `gh` on
PATH** (the Spark `server` topology). The `cli` path stays byte-identical (covered by test_create_issue.py /
test_view_issue.py); this file covers the seam builder, the native adapter (injected opener, network-free),
and the end-to-end gx10 gating/routing on the native path.
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
import forge_adapters  # noqa: E402
from forge_native import NativeForgeAdapter  # noqa: E402


# ── a network-free fake opener ────────────────────────────────────────────────
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
    """handler(method, url, req) -> obj (dict/list) OR raises urllib.error.HTTPError. `url` is the FULL
    request URL (path AND query), so a handler can key off `?page=…`."""
    def _open(req, timeout=None):
        return _Resp(handler(req.get_method(), req.full_url, req))
    return _open


def _http_error(code, msg="err"):
    return urllib.error.HTTPError("http://x", code, msg, {}, io.BytesIO(json.dumps({"message": msg}).encode()))


_ISSUE_REST = {
    "number": 1207, "state": "closed", "title": "flaky watchdog",
    "labels": [{"name": "type/bug"}, {"name": "area/ci"}],
    "milestone": {"title": "Phase X"}, "html_url": "https://github.com/o/r/issues/1207",
    "body": "the body", "id": 55501,
}


# ── seam builder ──────────────────────────────────────────────────────────────
def test_builder_defaults_to_cli():
    a = forge_adapters.build_forge_adapter(adapter="cli", repo="o/r", token="")
    assert a.name == "cli"


def test_builder_native_needs_token_then_repo():
    assert forge_adapters.build_forge_adapter(adapter="native", repo="o/r", token="").name == "native" \
        and not forge_adapters.build_forge_adapter(adapter="native", repo="o/r", token="").available()  # unavailable: no token
    assert not forge_adapters.build_forge_adapter(adapter="native", repo="", token="tok").available()   # unavailable: no repo
    a = forge_adapters.build_forge_adapter(adapter="native", repo="o/r", token="tok")
    assert a.name == "native" and a.available()


def test_builder_mock():
    assert forge_adapters.build_forge_adapter(adapter="mock", repo="", token="").name == "mock"


# ── native adapter (injected opener) ──────────────────────────────────────────
def test_native_view_issue_normalizes_rest_shape():
    a = NativeForgeAdapter("tok", "o/r", opener=_opener(lambda m, p, r: _ISSUE_REST))
    st, data = a.view_issue(1207)
    assert st == "ok"
    assert data["state"] == "CLOSED"                       # REST 'closed' → gh-style upper-case
    assert data["url"] == "https://github.com/o/r/issues/1207"   # html_url → url
    assert [l["name"] for l in data["labels"]] == ["type/bug", "area/ci"]
    assert data["milestone"] == {"title": "Phase X"} and data["body"] == "the body"


def test_native_view_issue_404_is_not_found():
    # the ISSUE endpoint 404s but the repo-root probe succeeds → a genuine (authoritative) not-found
    def h(method, url, req):
        if "/issues/" in url:
            raise _http_error(404, "Not Found")
        return {"full_name": "o/r"}
    st, _ = NativeForgeAdapter("tok", "o/r", opener=_opener(h)).view_issue(999999)
    assert st == "not_found"


def test_native_view_issue_repo_404_is_error_not_false_not_found():
    # both the issue AND the repo-root 404 (typo'd forge.repo / token lacks scope) → a real ERROR, NOT a
    # false authoritative NOT_FOUND — the #1208 regression guard on the native path.
    def h(method, url, req):
        raise _http_error(404, "Not Found")
    st, msg = NativeForgeAdapter("tok", "o/typo", opener=_opener(h)).view_issue(5)
    assert st == "error" and "repository" in msg.lower()


def test_native_link_sub_issue_resolves_child_number_and_posts():
    seen = {}

    def h(method, url, req):
        if method == "GET" and "/issues/42" in url:
            return {"id": 9001, "number": 42}
        if method == "POST" and "/sub_issues" in url:
            seen["body"] = json.loads(req.data.decode("utf-8"))
            seen["url"] = url
            return {}
        return {}
    # the seam passes the create-result DICT, not a bare int (the review-caught contract mismatch)
    st, _ = NativeForgeAdapter("tok", "o/r", opener=_opener(h)).link_sub_issue(
        "#1136", {"url": "https://x/issues/42", "number": 42})
    assert st == "ok"
    assert seen["body"] == {"sub_issue_id": 9001}          # keyed by the child's DATABASE id, not its number
    assert "/issues/1136/sub_issues" in seen["url"]        # posted under the parent


def test_native_view_issue_500_is_error():
    def h(m, p, r):
        raise _http_error(500, "boom")
    st, msg = NativeForgeAdapter("tok", "o/r", opener=_opener(h)).view_issue(5)
    assert st == "error" and "500" in msg


def test_native_create_issue_posts_and_returns_url():
    seen = {}

    def h(method, path, req):
        if method == "GET" and "/milestones" in path:
            return [{"title": "M1", "number": 3}]
        if method == "POST" and path.endswith("/issues"):
            seen["body"] = json.loads(req.data.decode("utf-8"))
            return {"html_url": "https://github.com/o/r/issues/42", "number": 42, "id": 9}
        return {}
    tmp = Path(__file__).with_name("_tmp_forge_body.md")
    tmp.write_text("# body\ncontent", encoding="utf-8")
    try:
        st, res = NativeForgeAdapter("tok", "o/r", opener=_opener(h)).create_issue(
            "My Issue", tmp, ["type/bug"], "M1")
    finally:
        tmp.unlink()
    assert st == "ok" and res["url"].endswith("/issues/42") and res["number"] == 42
    assert seen["body"]["title"] == "My Issue" and seen["body"]["labels"] == ["type/bug"]
    assert seen["body"]["milestone"] == 3                  # title 'M1' resolved to number 3
    assert seen["body"]["body"] == "# body\ncontent"


def test_native_list_labels_paginates():
    def h(method, url, req):
        # page 1 full (100) → forces a 2nd page; page 2 partial (2) → stop. Match `&page=1` precisely so it
        # does NOT also match the `per_page=100` substring.
        if "&page=1" in url:
            return [{"name": f"l{i}"} for i in range(100)]
        return [{"name": "type/bug"}, {"name": "area/ci"}]
    labels = NativeForgeAdapter("tok", "o/r", opener=_opener(h)).list_labels()
    assert labels is not None and "type/bug" in labels and "area/ci" in labels and len(labels) == 102


def test_native_ssrf_guard_refuses_non_github_host():
    # a mis-set api host must be refused before any request leaves
    a = NativeForgeAdapter("tok", "o/r", api="https://evil.example.com", opener=_opener(lambda *a: {}))
    st, msg = a.view_issue(1)
    assert st == "error" and "SSRF" in msg


# ── the keystone proof: gx10 offers + routes view_issue on native with NO gh ──
def test_view_issue_works_on_native_without_gh(monkeypatch, tmp_path):
    monkeypatch.setattr(gx10, "FORGE_ENABLED", True)
    monkeypatch.setattr(gx10, "FORGE_ADAPTER", "native")
    monkeypatch.setattr(gx10, "FORGE_REPO", "o/r")
    monkeypatch.setattr(gx10, "FORGE_TOKEN_ENV", "GX10_FORGE_TOKEN")
    monkeypatch.setenv("GX10_FORGE_TOKEN", "tok")
    monkeypatch.setattr(gx10, "_is_sealed_profile", lambda: False)
    monkeypatch.setattr(gx10.shutil, "which", lambda _: None)   # NO gh on PATH — the Spark case
    # the native adapter captures urllib.request.urlopen at construction → patch it to a fake
    monkeypatch.setattr(urllib.request, "urlopen", _opener(lambda m, p, r: _ISSUE_REST))

    assert gx10._forge_available() is True                       # usable via native, despite no gh
    assert "view_issue" in {t["function"]["name"] for t in gx10._effective_tools()}
    out = gx10.run_tool("view_issue", {"number": "#1207"})
    assert "#1207" in out and "[CLOSED]" in out and "type/bug" in out and "the body" in out


def test_native_gate_message_names_token_not_gh(monkeypatch):
    # with adapter=native and no token, the forge tool is NOT offered and the error names the token, not gh
    monkeypatch.setattr(gx10, "FORGE_ENABLED", True)
    monkeypatch.setattr(gx10, "FORGE_ADAPTER", "native")
    monkeypatch.setattr(gx10, "FORGE_REPO", "o/r")
    monkeypatch.setattr(gx10, "_is_sealed_profile", lambda: False)
    monkeypatch.delenv("GX10_FORGE_TOKEN", raising=False)
    assert "view_issue" not in {t["function"]["name"] for t in gx10._effective_tools()}
    out = gx10.run_tool("view_issue", {"number": "1"})
    assert out.startswith("ERROR: view_issue (native forge) needs a token")
