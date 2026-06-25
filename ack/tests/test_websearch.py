"""#459 (epic #440 P6, §4 / FORK-H) — first-class web search + current-info intent routing + the
execute_command shell guardrail. Three parts jointly fix the verified scaling-break bug (#447): a
current-info request made the model improvise a PowerShell `Invoke-WebRequest`, whose progress bar drew
into the renderer-owned conhost and corrupted the display.

Covers the gx10 surface: the web_search tool gating + handler, the pure shell guardrail + current-info
classifier, the proactive per-turn steer, and the PowerShell hardening. The dispatcher's web_search /
has_web_provider primitives are covered in test_dispatch.py.
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
import pytest  # noqa: E402


class _FakeDispatcher:
    """Minimal stand-in: has_web_provider gate + web_search result, recording its calls."""
    def __init__(self, web=True, result=None):
        self._web = web
        self._result = result or {"ok": True, "content": "SEARCH RESULTS: ...", "error": None,
                                  "provider_id": "codex-web"}
        self.calls = []

    def has_web_provider(self):
        return self._web

    def web_search(self, query, **kw):
        self.calls.append(query)
        return self._result


@pytest.fixture(autouse=True)
def _restore_dispatcher():
    prev = gx10._DISPATCHER
    yield
    gx10._DISPATCHER = prev


# ── tool gating (offered only with a web provider) ───────────────────────────
def test_web_search_tool_offered_only_with_a_web_provider(monkeypatch):
    monkeypatch.setattr(gx10, "_DISPATCHER", None)
    assert all(t["function"]["name"] != "web_search" for t in gx10._effective_tools())
    monkeypatch.setattr(gx10, "_DISPATCHER", _FakeDispatcher(web=False))
    assert all(t["function"]["name"] != "web_search" for t in gx10._effective_tools())   # no web cap → not offered
    monkeypatch.setattr(gx10, "_DISPATCHER", _FakeDispatcher(web=True))
    assert any(t["function"]["name"] == "web_search" for t in gx10._effective_tools())    # offered


# ── handler ──────────────────────────────────────────────────────────────────
def test_web_search_handler_runs_server_side_and_returns_content(monkeypatch):
    disp = _FakeDispatcher(web=True)
    monkeypatch.setattr(gx10, "_DISPATCHER", disp)
    out = gx10.run_tool("web_search", {"query": "aktuelle Lage X"})
    assert out == "SEARCH RESULTS: ..." and disp.calls == ["aktuelle Lage X"]


def test_web_search_handler_unavailable_without_web(monkeypatch):
    monkeypatch.setattr(gx10, "_DISPATCHER", _FakeDispatcher(web=False))
    out = gx10.run_tool("web_search", {"query": "x"})
    assert out.startswith("[web_search] unavailable")


def test_web_search_handler_reports_no_result(monkeypatch):
    monkeypatch.setattr(gx10, "_DISPATCHER", _FakeDispatcher(
        web=True, result={"ok": False, "content": None, "error": "no-capable-provider"}))
    out = gx10.run_tool("web_search", {"query": "x"})
    assert "[web_search] no result" in out and "no-capable-provider" in out


# ── current-info classifier (pure) ───────────────────────────────────────────
@pytest.mark.parametrize("q", [
    "was ist die aktuelle Lage", "latest news on X", "today's weather", "what is happening right now",
    "neueste Entwicklungen", "real-time price of Y",
])
def test_current_info_classifier_flags_recency(q):
    assert gx10._is_current_info_query(q) is True


@pytest.mark.parametrize("q", [
    "refactor this function", "explain the code", "write a test for the parser", "what does this regex do",
    # review A S3: bare "current" is everywhere in coding context — must NOT mis-steer
    "switch to the current branch", "print the current directory", "what is the current value of x",
    "refactor the current implementation",
    # review A (2nd round) S3: same for bare German "aktuell*" (current branch/value/file)
    "der aktuelle Branch", "den aktuellen Wert von x", "die aktuelle Datei lesen", "momentan läuft der Test",
])
def test_current_info_classifier_ignores_coding_queries(q):
    assert gx10._is_current_info_query(q) is False


# ── proactive steer (only when web is available) ─────────────────────────────
def test_steer_only_when_current_info_and_web_available(monkeypatch):
    monkeypatch.setattr(gx10, "_DISPATCHER", _FakeDispatcher(web=True))
    assert "web_search" in gx10._websearch_steer("was ist die aktuelle Lage")
    assert gx10._websearch_steer("refactor this function") == ""           # not current-info → no steer
    monkeypatch.setattr(gx10, "_DISPATCHER", _FakeDispatcher(web=False))
    assert gx10._websearch_steer("latest news") == ""                      # no web provider → no steer (no dead hint)
    monkeypatch.setattr(gx10, "_DISPATCHER", None)
    assert gx10._websearch_steer("latest news") == ""


# ── shell guardrail (pure) ───────────────────────────────────────────────────
@pytest.mark.parametrize("cmd,why", [
    ("Invoke-WebRequest https://x", "remote"), ("Invoke-RestMethod https://x", "remote"),
    ("iwr http://y", "remote"), ("irm http://y", "remote"), ("curl https://api.x", "remote"),
    ("wget http://z", "remote"), ("(New-Object Net.WebClient).DownloadString('http://x')", "remote"),
    ("Start-Sleep 99", "long-running"), ("while ($true) { ping x }", "long-running"),
    ("Get-Content app.log -Wait", "long-running"), ("ping -t host", "long-running"),
    ("Start-Job { x }", "long-running"),
])
def test_shell_guard_blocks_remote_and_unbounded(cmd, why):
    assert why in (gx10._shell_guard(cmd) or "")


@pytest.mark.parametrize("cmd", [
    "Get-Date", "Get-ChildItem", "Select-String foo *.txt", "echo hi", "git status",
    "Get-Content app.log", "python -c \"print(1)\"", "ls -la",
    # review A S3: a filename / search string that merely CONTAINS a fetch token must NOT be blocked
    "Select-String 'wget' app.log", "Get-Content curl.txt", "git clone https://github.com/u/curl",
])
def test_shell_guard_allows_normal_commands(cmd):
    assert gx10._shell_guard(cmd) is None


# ── execute_command: fail-closed block + web_search redirect + PowerShell hardening ──────────────────
def test_execute_command_blocks_web_fetch_and_redirects(monkeypatch):
    monkeypatch.setattr(gx10, "_DISPATCHER", _FakeDispatcher(web=True))
    out = gx10.run_tool("execute_command", {"command": "Invoke-WebRequest https://example.com"})
    assert out.startswith("BLOCKED") and "web_search" in out          # redirected to the tool (web available)


def test_execute_command_blocks_without_redirect_when_no_web(monkeypatch):
    monkeypatch.setattr(gx10, "_DISPATCHER", _FakeDispatcher(web=False))
    out = gx10.run_tool("execute_command", {"command": "curl https://x"})
    assert out.startswith("BLOCKED") and "web_search" not in out      # still blocked, but no dead redirect


def test_guard_fires_before_the_local_tool_bridge(monkeypatch):
    # review A S2: the guard must run SERVER-side before execute_command is delegated to a client bridge
    # (else a thin/Ink client would run the blocked command unguarded). A blocked command never reaches
    # the bridge; an allowed one passes through to it.
    calls = []
    monkeypatch.setattr(gx10, "_LOCAL_TOOL_BRIDGE", lambda name, args: calls.append((name, args)) or "BRIDGED")
    monkeypatch.setattr(gx10, "_DISPATCHER", _FakeDispatcher(web=True))
    out = gx10.run_tool("execute_command", {"command": "curl https://evil"})
    assert out.startswith("BLOCKED") and calls == []                 # blocked → never bridged to the client
    out2 = gx10.run_tool("execute_command", {"command": "Get-Date"})
    assert out2 == "BRIDGED" and calls == [("execute_command", {"command": "Get-Date"})]  # allowed → bridged


def test_execute_command_hardens_powershell_progress(monkeypatch):
    captured = {}
    def fake_run(argv, **kw):
        captured["argv"] = argv
        return types.SimpleNamespace(stdout="ok", stderr="", returncode=0)
    monkeypatch.setattr(gx10, "PLATFORM", "windows")
    monkeypatch.setattr(gx10.subprocess, "run", fake_run)
    monkeypatch.setattr(gx10, "_DISPATCHER", _FakeDispatcher(web=True))
    gx10.run_tool("execute_command", {"command": "Get-Date"})
    joined = " ".join(captured["argv"])
    assert "$ProgressPreference='SilentlyContinue';" in joined and "Get-Date" in joined
