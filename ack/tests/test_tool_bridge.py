"""Client tool bridge: the orchestrator passes code-tools THROUGH to the driving client.

Covers the rendezvous (server emits a request, blocks; client posts the result; turn
resumes) and the engine routing (LOCAL_TOOL_NAMES go to the bridge when active, else run
server-side). The full streamed round-trip is exercised live (see the live smoke).
"""
from __future__ import annotations

import json
import sys
import threading
import types
import time
from pathlib import Path

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=object))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402
import server  # noqa: E402
import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_bridge():
    yield
    gx10._LOCAL_TOOL_BRIDGE = None
    server._ACTIVE_BRIDGE["b"] = None


# ── ToolBridge rendezvous ────────────────────────────────────
def test_bridge_request_emits_frame_and_waits_for_result():
    frames = []
    bridge = server.ToolBridge(frames.append, timeout=5)
    result_box = {}

    def _ask():
        result_box["r"] = bridge.request("read_file", {"path": "a.py"})

    t = threading.Thread(target=_ask)
    t.start()
    # the request emits a single \x00TR{json}\x00 frame and then blocks
    for _ in range(50):
        if frames:
            break
        time.sleep(0.02)
    assert len(frames) == 1
    frame = frames[0]
    assert frame.startswith(server._TR_PREFIX) and frame.endswith(server._TR_SUFFIX)
    assert "\x00" not in frame[len(server._TR_PREFIX):-len(server._TR_SUFFIX)]   # clean json
    payload = json.loads(frame[len(server._TR_PREFIX):-len(server._TR_SUFFIX)])
    assert payload["name"] == "read_file" and payload["args"] == {"path": "a.py"}
    # delivering the result unblocks the waiting request
    assert bridge.deliver(payload["id"], "file contents") is True
    t.join(2)
    assert result_box["r"] == "file contents"


def test_bridge_timeout_returns_error():
    bridge = server.ToolBridge(lambda _f: None, timeout=0.2)
    out = bridge.request("execute_command", {"command": "x"})
    assert out.startswith("ERROR") and "timed out" in out


def test_deliver_unknown_id_is_false():
    bridge = server.ToolBridge(lambda _f: None)
    assert bridge.deliver("nope", "x") is False


def test_bridge_is_callable():
    # the engine calls _LOCAL_TOOL_BRIDGE(name, args) → the object MUST be callable
    frames = []
    bridge = server.ToolBridge(frames.append, timeout=0.2)
    out = bridge("read_file", {"path": "x"})   # __call__ → request → (times out here)
    assert frames and out.startswith("ERROR") and "timed out" in out


# ── engine routing ───────────────────────────────────────────
def test_local_tool_routes_to_bridge_when_active():
    gx10._LOCAL_TOOL_BRIDGE = lambda name, args: f"BRIDGED:{name}:{args.get('path')}"
    assert gx10.run_tool("read_file", {"path": "x.py"}) == "BRIDGED:read_file:x.py"
    assert gx10.run_tool("execute_command", {"command": "ls"}) == "BRIDGED:execute_command:None"


def test_nonlocal_tool_not_routed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    gx10._LOCAL_TOOL_BRIDGE = lambda name, args: "SHOULD_NOT_HAPPEN"
    # query_memory is server-side; with no memory it reports unavailable, never bridged
    out = gx10.run_tool("query_memory", {"query": "x"})
    assert "SHOULD_NOT_HAPPEN" not in out


def test_no_bridge_runs_server_side(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    gx10._LOCAL_TOOL_BRIDGE = None
    assert gx10.run_tool("read_file", {"path": "missing.py"}).startswith("ERROR: Not found")
    (tmp_path / "f.txt").write_text("hi", encoding="utf-8")
    assert gx10.run_tool("read_file", {"path": "f.txt"}) == "hi"
