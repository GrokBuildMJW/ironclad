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


from websearch_adapters import CliDelegateAdapter, MockAdapter   # noqa: E402


@pytest.fixture(autouse=True)
def _restore_dispatcher():
    prev_d, prev_w = gx10._DISPATCHER, gx10._WEBSEARCH
    yield
    gx10._DISPATCHER, gx10._WEBSEARCH = prev_d, prev_w


def _wire(monkeypatch, disp):
    """Wire the web_search seam (epic #505 S3) to a CLI-delegate over the fake dispatcher."""
    monkeypatch.setattr(gx10, "_DISPATCHER", disp)
    monkeypatch.setattr(gx10, "_WEBSEARCH", CliDelegateAdapter(disp) if disp is not None else None)
    return disp


# ── tool gating (offered only when a usable adapter is available) ─────────────
def test_web_search_tool_offered_only_when_adapter_available(monkeypatch):
    monkeypatch.setattr(gx10, "_WEBSEARCH", None)
    assert all(t["function"]["name"] != "web_search" for t in gx10._effective_tools())
    monkeypatch.setattr(gx10, "_WEBSEARCH", CliDelegateAdapter(_FakeDispatcher(web=False)))
    assert all(t["function"]["name"] != "web_search" for t in gx10._effective_tools())   # cli adapter not available
    monkeypatch.setattr(gx10, "_WEBSEARCH", CliDelegateAdapter(_FakeDispatcher(web=True)))
    assert any(t["function"]["name"] == "web_search" for t in gx10._effective_tools())    # offered


def test_web_search_offered_without_a_dispatcher_when_adapter_is_standalone(monkeypatch):
    # epic #505 #1 acceptance: a native/mock adapter offers web_search even with NO provider lane.
    monkeypatch.setattr(gx10, "_DISPATCHER", None)
    monkeypatch.setattr(gx10, "_WEBSEARCH", MockAdapter())
    assert any(t["function"]["name"] == "web_search" for t in gx10._effective_tools())


# ── handler (runs through the standalone seam) ───────────────────────────────
def test_web_search_handler_runs_through_the_seam(monkeypatch):
    disp = _wire(monkeypatch, _FakeDispatcher(web=True))
    out = gx10.run_tool("web_search", {"query": "aktuelle Lage X"})
    assert "SEARCH RESULTS: ..." in out and disp.calls == ["aktuelle Lage X"]
    assert 'Web search results for: "aktuelle Lage X"' in out   # structured SearchOutput rendering


def test_web_search_handler_unavailable_without_web(monkeypatch):
    _wire(monkeypatch, _FakeDispatcher(web=False))
    out = gx10.run_tool("web_search", {"query": "xy"})
    assert out.startswith("[web_search] unavailable")


def test_web_search_handler_reports_no_result(monkeypatch):
    _wire(monkeypatch, _FakeDispatcher(
        web=True, result={"ok": False, "content": None, "error": "no-capable-provider"}))
    out = gx10.run_tool("web_search", {"query": "xy"})
    assert "[web_search] no result" in out and "no-capable-provider" in out


# ── S2 (#507): tool schema + Validate->Reask in the handler ──────────────────
def test_web_search_schema_exposes_grammar_clean_domain_filters():
    props = gx10.WEBSEARCH_TOOL["function"]["parameters"]["properties"]
    assert "allowDomains" in props and "blockDomains" in props
    for k in ("allowDomains", "blockDomains"):
        assert props[k]["type"] == "array" and props[k]["items"]["type"] == "string"
    assert gx10.WEBSEARCH_TOOL["function"]["parameters"]["required"] == ["query"]
    # grammar-clean: no constructs XGrammar V1 rejects (the strict rules live in the validator)
    import json
    blob = json.dumps(gx10.WEBSEARCH_TOOL)
    for banned in ("minLength", "pattern", "minItems", "maxItems", "oneOf", "anyOf"):
        assert banned not in blob


def test_web_search_handler_rejects_short_query_without_dispatch(monkeypatch):
    disp = _wire(monkeypatch, _FakeDispatcher(web=True))
    out = gx10.run_tool("web_search", {"query": "a"})
    assert "at least 2" in out and disp.calls == []          # reask, never dispatched


def test_web_search_handler_rejects_allow_and_block(monkeypatch):
    disp = _wire(monkeypatch, _FakeDispatcher(web=True))
    out = gx10.run_tool("web_search",
                        {"query": "hello", "allowDomains": ["a.com"], "blockDomains": ["b.com"]})
    assert "mutually exclusive" in out and disp.calls == []


def test_web_search_handler_rejects_wildcard_domain(monkeypatch):
    disp = _wire(monkeypatch, _FakeDispatcher(web=True))
    out = gx10.run_tool("web_search", {"query": "hello", "allowDomains": ["*.foo.com"]})
    assert "wildcard" in out.lower() and disp.calls == []


def test_web_search_handler_accepts_and_normalizes_then_dispatches(monkeypatch):
    disp = _wire(monkeypatch, _FakeDispatcher(web=True))
    # query is trimmed; domains are accepted here and threaded into the adapter in S4 (#509)
    out = gx10.run_tool("web_search", {"query": "  hello  ", "allowDomains": ["HTTPS://Example.com/x"]})
    assert "SEARCH RESULTS: ..." in out and disp.calls == ["hello"]


def test_web_search_handler_appends_sources_and_reminder(monkeypatch):
    # S5 (#510): every web_search result ends with the deterministic sources reminder, and a
    # hit-bearing adapter contributes a Sources list.
    from websearch_adapters import _SOURCES_REMINDER
    monkeypatch.setattr(gx10, "_WEBSEARCH", MockAdapter())
    out = gx10.run_tool("web_search", {"query": "latest ai news"})
    assert out.endswith(_SOURCES_REMINDER) and "Sources:" in out and "http" in out


# ── S6 (#511): prompt + tool-description refresh ─────────────────────────────
def test_steer_fires_under_a_native_adapter(monkeypatch):
    # epic #505 R1: the current-info steer must fire under a native (non-CLI) adapter too — the
    # availability gate is adapter-aware, not dispatcher-only.
    monkeypatch.setattr(gx10, "_DISPATCHER", None)
    monkeypatch.setattr(gx10, "_WEBSEARCH", MockAdapter())
    assert "web_search" in gx10._websearch_steer("what is the latest ai news")


def test_tool_description_mentions_domains_and_sources():
    desc = gx10.WEBSEARCH_TOOL["function"]["description"].lower()
    assert "domains" in desc and "sources" in desc


def test_orchestrator_prompt_has_web_search_sources_rule():
    import pathlib
    p = pathlib.Path(gx10.__file__).resolve().parent / "prompts" / "GX10_Orchestrator_SystemPrompt.md"
    text = p.read_text(encoding="utf-8")
    assert "web_search" in text and "Sources:" in text


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
    monkeypatch.setattr(gx10, "_WEBSEARCH", CliDelegateAdapter(_FakeDispatcher(web=True)))
    assert "web_search" in gx10._websearch_steer("was ist die aktuelle Lage")
    assert gx10._websearch_steer("refactor this function") == ""           # not current-info → no steer
    monkeypatch.setattr(gx10, "_WEBSEARCH", CliDelegateAdapter(_FakeDispatcher(web=False)))
    assert gx10._websearch_steer("latest news") == ""                      # adapter not available → no dead hint
    monkeypatch.setattr(gx10, "_WEBSEARCH", None)
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
    _wire(monkeypatch, _FakeDispatcher(web=True))
    out = gx10.run_tool("execute_command", {"command": "Invoke-WebRequest https://example.com"})
    assert out.startswith("BLOCKED") and "web_search" in out          # redirected to the tool (web available)


def test_execute_command_blocks_without_redirect_when_no_web(monkeypatch):
    _wire(monkeypatch, _FakeDispatcher(web=False))
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


def test_execute_command_windows_refuses_before_powershell(monkeypatch):
    calls = []
    monkeypatch.setattr(gx10, "PLATFORM", "windows")
    monkeypatch.setattr(gx10.subprocess, "run", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(gx10, "_DISPATCHER", _FakeDispatcher(web=True))
    out = gx10.run_tool("execute_command", {"command": "Get-Date"})
    assert out.startswith("ERROR: execute_command refused") and "Windows" in out and "fails closed" in out
    assert calls == []
