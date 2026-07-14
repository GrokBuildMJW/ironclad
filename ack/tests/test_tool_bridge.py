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
import urllib.error
from pathlib import Path

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=object))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402
import server  # noqa: E402
import client  # noqa: E402
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
    frames = []
    bridge = server.ToolBridge(frames.append, timeout=0.2)
    out = bridge.request("execute_command", {"command": "x"})
    assert out.startswith("ERROR") and "timed out" in out
    payload = json.loads(frames[0][len(server._TR_PREFIX):-len(server._TR_SUFFIX)])
    assert payload["name"] == "execute_command_sandboxed_v1"
    assert payload["sandbox"] in {"auto", "bwrap", "firejail"}


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


def test_bridge_frame_ships_exec_cwd_only_for_non_default_project(monkeypatch, tmp_path):
    # #1317: the TR frame ships the active project's exec cwd ONLY for a genuinely non-default project (so a
    # bridged tool runs THERE); the DEFAULT project OMITS it → the client keeps its own cwd (byte-identical).
    import project_context as _pc
    from project_context import ProjectContext
    monkeypatch.setattr(gx10, "_BOOT_WORKDIR", Path(str(tmp_path / "boot")))

    def _frame(root):
        frames = []
        bridge = server.ToolBridge(frames.append, timeout=5)

        def _run():
            if root is not None:
                _pc.set_current(ProjectContext("p", root, ""))   # bind on THIS request thread (≙ bind_active)
            bridge.request("read_file", {"path": "a.py"})

        t = threading.Thread(target=_run)
        t.start()
        for _ in range(50):
            if frames:
                break
            time.sleep(0.02)
        payload = json.loads(frames[0][len(server._TR_PREFIX):-len(server._TR_SUFFIX)])
        bridge.deliver(payload["id"], "x")
        t.join(2)
        return payload

    assert "exec_cwd" not in _frame(None)                        # default (no non-default ctx) → omitted
    assert _frame(str(tmp_path))["exec_cwd"] == str(tmp_path)     # non-default project → its root shipped


def test_python_client_maps_versioned_exec_and_carries_sandbox_policy(monkeypatch):
    calls = []
    posted = []
    monkeypatch.setattr(gx10, "run_tool", lambda name, args, **kw: calls.append((name, args, kw)) or "OK")
    cli = client.Server.__new__(client.Server)
    cli._req = lambda method, path, body: posted.append((method, path, body)) or {}
    frame = "TR" + json.dumps({
        "id": "e1", "name": "execute_command_sandboxed_v1", "args": {"command": "echo hi"},
        "sandbox": "firejail", "exec_cwd": "/project",
    })
    cli._run_passthrough_tool(frame)
    assert calls == [("execute_command", {"command": "echo hi"},
                      {"exec_cwd": "/project", "sandbox_policy": "firejail"})]
    assert posted == [("POST", "/tool-result", {"id": "e1", "result": "OK"})]


def test_python_client_retries_transient_tool_result_post(monkeypatch):
    attempts = []
    sleeps = []
    monkeypatch.setattr(gx10, "run_tool", lambda _name, _args, **_kw: "OK")
    monkeypatch.setattr(client.time, "sleep", sleeps.append)
    cli = client.Server.__new__(client.Server)

    def _req(method, path, body):
        attempts.append((method, path, body))
        if len(attempts) == 1:
            raise urllib.error.URLError("temporary network failure")
        return {}

    cli._req = _req
    cli._run_passthrough_tool('TR{"id":"r1","name":"read_file","args":{"path":"a"}}')
    assert attempts == [
        ("POST", "/tool-result", {"id": "r1", "result": "OK"}),
        ("POST", "/tool-result", {"id": "r1", "result": "OK"}),
    ]
    assert sleeps == [client._TOOL_RESULT_POST_BACKOFF_S]


def test_python_client_retries_read_phase_socket_error(monkeypatch):
    # #1490 (Opus review): a read-phase ConnectionResetError / socket.timeout on resp.read() is NOT a
    # urllib.error.URLError subclass, so the retry must catch OSError — else it escapes _run_passthrough_tool
    # and breaks the very stream this fix protects. Assert such an error is retried, not propagated.
    attempts = []
    monkeypatch.setattr(gx10, "run_tool", lambda _name, _args, **_kw: "OK")
    monkeypatch.setattr(client.time, "sleep", lambda _seconds: None)
    cli = client.Server.__new__(client.Server)

    def _req(method, path, body):
        attempts.append((method, path, body))
        if len(attempts) == 1:
            raise ConnectionResetError("connection reset while reading the response body")
        return {}

    cli._req = _req
    cli._run_passthrough_tool('TR{"id":"r4","name":"read_file","args":{"path":"a"}}')  # must NOT raise
    assert len(attempts) == 2  # first attempt reset → retried, second delivered


def test_python_client_drops_permanent_tool_result_rejection_without_retry(monkeypatch):
    attempts = []
    monkeypatch.setattr(gx10, "run_tool", lambda _name, _args, **_kw: "OK")
    monkeypatch.setattr(client.time, "sleep", lambda _seconds: pytest.fail("must not back off for 4xx"))
    cli = client.Server.__new__(client.Server)

    def _req(method, path, body):
        attempts.append((method, path, body))
        raise urllib.error.HTTPError(path, 410, "Gone", None, None)

    cli._req = _req
    cli._run_passthrough_tool('TR{"id":"r2","name":"read_file","args":{"path":"a"}}')
    assert len(attempts) == 1


def test_python_client_drops_tool_result_after_bounded_transient_retries(monkeypatch):
    attempts = []
    sleeps = []
    monkeypatch.setattr(gx10, "run_tool", lambda _name, _args, **_kw: "OK")
    monkeypatch.setattr(client.time, "sleep", sleeps.append)
    cli = client.Server.__new__(client.Server)

    def _req(method, path, body):
        attempts.append((method, path, body))
        raise urllib.error.URLError("persistent network failure")

    cli._req = _req
    cli._run_passthrough_tool('TR{"id":"r3","name":"read_file","args":{"path":"a"}}')
    assert len(attempts) == client._TOOL_RESULT_POST_ATTEMPTS
    assert sleeps == [
        client._TOOL_RESULT_POST_BACKOFF_S,
        client._TOOL_RESULT_POST_BACKOFF_S * 2,
        client._TOOL_RESULT_POST_BACKOFF_S * 3,
    ]


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
