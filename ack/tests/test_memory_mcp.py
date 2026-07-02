"""#480 (epic #440 Phase 6 / FORK-G D2) — read-only Memory MCP (stdio) for the external coding CLIs.

Covers the dependency-free stdio MCP server (`engine/memory_mcp.py`): the JSON-RPC protocol
(initialize / tools/list / tools/call / notifications), the read-only tool set (search + deep_query, NO
write), fail-soft behaviour, the serve() loop framing, and the always-on launch renderer `render_mcp_launch`
(#994-S10: read-only Memory MCP when memory + per-CLI mcp_template, any profile; secret-free env, forward-slash path).
"""
from __future__ import annotations

import io
import json
import sys
import types
from pathlib import Path

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=object))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import memory_mcp  # noqa: E402
import pytest  # noqa: E402


class _FakeMem:
    def __init__(self, hits=None, deep="[Memory] graph matches:\n- X depends on Y", boom=False):
        self._hits = hits if hits is not None else ["past decision X", "gotcha Y"]
        self._deep = deep
        self._boom = boom

    def search(self, q, limit):
        if self._boom:
            raise RuntimeError("mem down")
        return self._hits[:limit]

    def deep_query(self, q, limit):
        if self._boom:
            raise RuntimeError("mem down")
        return self._deep


def _req(method, _id=1, **params):
    r = {"jsonrpc": "2.0", "method": method}
    if _id is not None:
        r["id"] = _id
    if params:
        r["params"] = params
    return r


# ── protocol ─────────────────────────────────────────────────────────────────
def test_initialize_returns_server_info_and_capabilities():
    r = memory_mcp.handle_request(_req("initialize"), _FakeMem())
    assert r["result"]["protocolVersion"] == memory_mcp.PROTOCOL_VERSION
    assert r["result"]["serverInfo"]["name"] == "ironclad-memory"
    assert "tools" in r["result"]["capabilities"]


def test_tools_list_is_read_only():
    r = memory_mcp.handle_request(_req("tools/list"), _FakeMem())
    names = {t["name"] for t in r["result"]["tools"]}
    assert names == {"memory_search", "memory_deep_query"}      # NO write tool — write-back deferred


def test_tools_call_search_and_deep_query():
    s = memory_mcp.handle_request(_req("tools/call", name="memory_search",
                                       arguments={"query": "how X", "limit": 2}), _FakeMem())
    assert "past decision X" in s["result"]["content"][0]["text"]
    d = memory_mcp.handle_request(_req("tools/call", name="memory_deep_query",
                                       arguments={"query": "deps"}), _FakeMem())
    assert "graph matches" in d["result"]["content"][0]["text"]


def test_unknown_tool_is_rejected():
    r = memory_mcp.handle_request(_req("tools/call", name="memory_write", arguments={}), _FakeMem())
    assert r["error"]["code"] == -32602 and "memory_write" in r["error"]["message"]


def test_unknown_method_is_method_not_found():
    assert memory_mcp.handle_request(_req("frobnicate"), _FakeMem())["error"]["code"] == -32601


def test_notification_gets_no_response():
    assert memory_mcp.handle_request({"jsonrpc": "2.0", "method": "notifications/initialized"}, _FakeMem()) is None
    # a request without an id (other than initialize) is a notification → no reply
    assert memory_mcp.handle_request({"jsonrpc": "2.0", "method": "tools/list"}, _FakeMem()) is None


def test_empty_query_and_fail_soft():
    empty = memory_mcp.handle_request(_req("tools/call", name="memory_search", arguments={"query": " "}), _FakeMem())
    assert "required" in empty["result"]["content"][0]["text"]
    boom = memory_mcp.handle_request(_req("tools/call", name="memory_search",
                                          arguments={"query": "x"}), _FakeMem(boom=True))
    assert "unavailable" in boom["result"]["content"][0]["text"]   # a memory hiccup never crashes the server


def test_serve_loop_skips_garbage_and_replies_per_line():
    inp = io.StringIO("\n".join([
        json.dumps(_req("initialize", 1)), "  ", "not json{", json.dumps(_req("tools/list", 2)),
    ]) + "\n")
    out = io.StringIO()
    memory_mcp.serve(stdin=inp, stdout=out, mem=_FakeMem())
    ids = [json.loads(l)["id"] for l in out.getvalue().splitlines()]
    assert ids == [1, 2]                                          # blank + malformed frames skipped


# ── gated launch renderer ────────────────────────────────────────────────────
_TMPL = '--mcp-config \'{"mcpServers":{"memory":{"command":"{mcp_cmd}","args":["{mcp_script}"]}}}\''


def test_render_mcp_launch_is_always_on_when_configured():
    # #994-S10: the read-only Memory MCP is ALWAYS ON when memory + a per-CLI template — no longer
    # sealed-gated (the profile no longer matters; a coder can only READ memory, never write).
    for sealed in (False, True):
        args, env = memory_mcp.render_mcp_launch(_TMPL, sealed=sealed, memory_url="http://mem:8800",
                                                 namespace="ironclad", py="python", path="C:/x/memory_mcp.py")
        assert '"command":"python"' in args and '"args":["C:/x/memory_mcp.py"]' in args   # forward-slash → valid JSON
        assert env == {"GX10_MEMORY_URL": "http://mem:8800", "GX10_MCP_MEMORY_NS": "ironclad"}
    # OFF only when memory is unconfigured OR the agent has no template → byte-identical launch (no args/env)
    assert memory_mcp.render_mcp_launch(_TMPL, memory_url="", namespace="ns") == ("", {})
    assert memory_mcp.render_mcp_launch(None, memory_url="http://mem:8800", namespace="ns") == ("", {})


def test_render_mcp_launch_normalizes_windows_path():
    args, _ = memory_mcp.render_mcp_launch(_TMPL, sealed=True, memory_url="http://m", namespace="ns",
                                           py="python", path="C:\\Users\\x\\memory_mcp.py")
    assert "C:/Users/x/memory_mcp.py" in args and "\\" not in args   # backslashes would break the JSON


def test_memory_from_env_uses_gx10_memory_agent(monkeypatch):
    # MEM-1 (#503): the agent_id falls back to GX10_MEMORY_AGENT (the engine's real knob, gx10:_apply_config)
    # — NOT the never-set GX10_MEMORY_AGENT_ID (which silently landed on the "ironclad" default).
    monkeypatch.delenv("GX10_MCP_MEMORY_NS", raising=False)
    monkeypatch.delenv("GX10_MEMORY_AGENT_ID", raising=False)
    monkeypatch.setenv("GX10_MEMORY_AGENT", "proj-x")
    assert memory_mcp.memory_from_env().agent_id == "proj-x"
