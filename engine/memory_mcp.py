"""Read-only Memory MCP server (#480, epic #440 Phase 6 / FORK-G D2).

A minimal, dependency-free **stdio MCP server** (JSON-RPC 2.0, newline-delimited) that exposes the
project memory to an external coding CLI (Codex/Claude) as **read-only** tools, so the agent can query the
same knowledge the orchestrator has during a handover. Spawned as a subprocess by the MCP-capable CLI; the
code-agent registry injects the per-CLI MCP config (and this script's memory connection) at launch — and
ONLY under the ``sealed`` trust profile (operator 2026-06-25).

Design:
- **secret-free wire**: the memory connection (base_url + the PROJECT namespace agent_id) is read from the
  ENV the launcher set (``GX10_MEMORY_URL`` / ``GX10_MCP_MEMORY_NS``), never from the JSON-RPC wire.
- **read-only**: only ``memory_search`` (vector) and ``memory_deep_query`` (relational/graph). No write
  tool — write-back is deferred (FORK-G).
- **project-namespaced** (operator): reads the configured project memory agent_id (default ``ironclad``),
  i.e. the SAME namespace the orchestrator + the #458 handover brief use — not a per-code-agent namespace.
- **fail-soft**: a memory hiccup returns an MCP tool result describing the miss, never crashes the server;
  an unknown method returns a JSON-RPC error; malformed input is skipped.

The protocol handling (``handle_request``) is a pure function over a parsed request + a memory client, so
it is unit-tested offline with a fake memory client and no subprocess/stdio.
"""
from __future__ import annotations

import json
import os
import shlex
import sys
from typing import Any, Dict, List, Optional, Tuple

# Stdlib-only + importable without the package: load the sibling MemoryManager by path when run as a
# script (the CLI spawns `python memory_mcp.py`), else via a normal import for the test suite.
try:
    from memory import MemoryManager           # type: ignore
except Exception:  # noqa: BLE001 — running as a bare script: add our own dir to sys.path
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from memory import MemoryManager           # type: ignore

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "ironclad-memory", "version": "0.1.0"}
_SEARCH_LIMIT_CAP = 20                          # never let a caller request an unbounded result set

# ── tool catalogue (read-only) ───────────────────────────────────────────────────────────────────
TOOLS: List[Dict[str, Any]] = [
    {
        "name": "memory_search",
        "description": ("Search the project memory (vector / semantic) for relevant past decisions, "
                        "gotchas and context. READ-ONLY."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "natural-language query"},
                "limit": {"type": "integer", "description": "max results (default 5)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "memory_deep_query",
        "description": ("Relational / multi-hop memory query over the knowledge graph (e.g. 'what depends "
                        "on X', 'how are A and B connected'). Slower than memory_search. READ-ONLY."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "relational query"},
                "limit": {"type": "integer", "description": "max results (default 5)"},
            },
            "required": ["query"],
        },
    },
]
_TOOL_NAMES = {t["name"] for t in TOOLS}


def server_path() -> str:
    """Absolute path to this script — the command an MCP-capable CLI spawns."""
    return os.path.abspath(__file__)


def render_mcp_launch(mcp_template: Optional[str], *, sealed: bool, memory_url: str,
                      namespace: str, py: str = "", path: str = "") -> Tuple[str, Dict[str, str]]:
    """#480: the GATED Memory-MCP launch args + env for one code agent. The Memory MCP is wired into the
    agent's launch ONLY when **all** hold: the trust profile is ``sealed`` (operator 2026-06-25), a memory
    service is configured (``memory_url``), and the agent ships a per-CLI ``mcp_template``. Returns
    ``(mcp_args, mcp_env)`` — ``("", {})`` when not gated on (so the agent launches byte-identically to
    today). The ``{mcp_server}`` token in the template renders to the python invocation of this script; the
    memory connection travels in ``mcp_env`` (inherited by the spawned MCP), NEVER on the MCP wire
    (secret-free). Pure → unit-tested."""
    if not (sealed and memory_url and mcp_template):
        return "", {}
    cmd = (py or sys.executable).replace("\\", "/")
    script = (path or server_path()).replace("\\", "/")   # forward slashes: valid inside a JSON/TOML
                                                          # mcp_template AND runnable by Windows Python
    mcp_args = (mcp_template
                # granular tokens so a per-CLI template can compose the spawn however it needs:
                #   Claude --mcp-config JSON: {"command":"{mcp_cmd}","args":["{mcp_script}"]}
                #   Codex -c: mcp_servers.memory.command="{mcp_cmd}" ... args=["{mcp_script}"]
                .replace("{mcp_cmd}", cmd)
                .replace("{mcp_script}", script)
                # combined convenience token for simple shells
                .replace("{mcp_server}", f"{shlex.quote(cmd)} {shlex.quote(script)}"))
    env = {"GX10_MEMORY_URL": memory_url, "GX10_MCP_MEMORY_NS": namespace or "ironclad"}
    return mcp_args, env


def memory_from_env() -> MemoryManager:
    """Build the read memory client from the launcher's ENV (secret-free wire). The PROJECT namespace
    (operator decision) is the configured memory agent_id, NOT the code-agent id — so the CLI queries the
    same knowledge the orchestrator built."""
    return MemoryManager({
        "base_url": os.environ.get("GX10_MEMORY_URL", ""),
        # MEM-1 (#503): fall back to GX10_MEMORY_AGENT — the SAME knob the engine reads (gx10:_apply_config) —
        # not GX10_MEMORY_AGENT_ID, which nothing ever sets (so the fallback silently landed on "ironclad").
        "agent_id": os.environ.get("GX10_MCP_MEMORY_NS") or os.environ.get("GX10_MEMORY_AGENT") or "ironclad",
        "read_timeout": float(os.environ.get("GX10_MCP_READ_TIMEOUT", "15") or 15),
    })


def _ok(req_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _err(req_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _text_result(text: str) -> Dict[str, Any]:
    """An MCP tool result: a single text content block."""
    return {"content": [{"type": "text", "text": text}]}


def _run_tool(name: str, args: Dict[str, Any], mem: MemoryManager) -> Dict[str, Any]:
    """Run a read-only memory tool. Fail-soft → a descriptive text result, never raises."""
    query = str((args or {}).get("query") or "").strip()
    if not query:
        return _text_result("[memory] error: 'query' is required.")
    try:
        limit = int((args or {}).get("limit", 5))
    except (TypeError, ValueError):
        limit = 5
    limit = max(1, min(limit, _SEARCH_LIMIT_CAP))
    try:
        if name == "memory_search":
            hits = mem.search(query, limit)
            if not hits:
                return _text_result("[memory] no relevant matches.")
            return _text_result("[memory] matches:\n" + "\n".join(f"- {h}" for h in hits))
        # memory_deep_query
        return _text_result(mem.deep_query(query, limit))
    except Exception as e:  # noqa: BLE001 — a memory hiccup must not crash the MCP server
        return _text_result(f"[memory] unavailable ({e!r}).")


def handle_request(req: Dict[str, Any], mem: MemoryManager) -> Optional[Dict[str, Any]]:
    """Dispatch ONE JSON-RPC request. Returns the response dict, or None for a notification (no reply).
    Pure over (req, mem) — unit-tested with a fake memory client."""
    if not isinstance(req, dict):
        return None
    method = req.get("method")
    req_id = req.get("id")
    # notifications (no id) get no response
    if method == "notifications/initialized" or (req_id is None and method != "initialize"):
        return None
    if method == "initialize":
        return _ok(req_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        })
    if method == "tools/list":
        return _ok(req_id, {"tools": TOOLS})
    if method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name")
        if name not in _TOOL_NAMES:
            return _err(req_id, -32602, f"unknown tool: {name!r}")
        return _ok(req_id, _run_tool(name, params.get("arguments") or {}, mem))
    if method in ("ping",):
        return _ok(req_id, {})
    return _err(req_id, -32601, f"method not found: {method!r}")


def serve(stdin=None, stdout=None, mem: Optional[MemoryManager] = None) -> None:
    """The stdio loop: newline-delimited JSON-RPC in → responses out. Each line is one message; a blank
    line / parse error is skipped (never crash the server). Injectable streams + memory for testing."""
    rin = stdin if stdin is not None else sys.stdin
    rout = stdout if stdout is not None else sys.stdout
    mc = mem if mem is not None else memory_from_env()
    for line in rin:
        line = (line or "").strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except (ValueError, TypeError):
            continue                            # malformed frame → skip, keep serving
        resp = handle_request(req, mc)
        if resp is not None:
            rout.write(json.dumps(resp) + "\n")
            rout.flush()


if __name__ == "__main__":  # pragma: no cover — exercised via serve()/handle_request() in tests
    serve()
