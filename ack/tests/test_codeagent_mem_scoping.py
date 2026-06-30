"""Code-agent memory scoping: MCP env and reducer writes respect active project mem_ns."""
from __future__ import annotations

import sys
import threading
import time
import types
from pathlib import Path

import pytest

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import project_context as pc  # noqa: E402
from project_context import ProjectContext  # noqa: E402
import gx10  # noqa: E402
import memory  # noqa: E402


def test_mcp_for_launch_namespaces_to_active_project(monkeypatch) -> None:
    monkeypatch.setenv("GX10_PROFILE", "sealed")
    monkeypatch.setattr(
        gx10, "_MEMORY_CONFIG", {"base_url": "http://mem:8800", "agent_id": "ironclad"}
    )
    spec = types.SimpleNamespace(
        mcp_template='{"command":"{mcp_cmd}","args":["{mcp_script}"]}'
    )

    ctx = ProjectContext("p", "/r", "deadbeefcafe1234")
    assert pc.current() is None
    with pc.use(ctx):
        args, env = gx10._mcp_for_launch(spec)
        assert env.get("GX10_MCP_MEMORY_NS") == "deadbeefcafe1234"
        assert args
    assert pc.current() is None


def test_mcp_for_launch_defaults_to_agent_id_without_project(monkeypatch) -> None:
    monkeypatch.setenv("GX10_PROFILE", "sealed")
    monkeypatch.setattr(
        gx10, "_MEMORY_CONFIG", {"base_url": "http://mem:8800", "agent_id": "ironclad"}
    )
    spec = types.SimpleNamespace(
        mcp_template='{"command":"{mcp_cmd}","args":["{mcp_script}"]}'
    )

    assert pc.current() is None
    args, env = gx10._mcp_for_launch(spec)
    assert env.get("GX10_MCP_MEMORY_NS") == "ironclad"
    assert args


def _make_capture():
    captured: list[dict] = []
    lock = threading.Lock()

    def fake_post(path: str, body: dict, timeout: float) -> dict:
        with lock:
            captured.append(body)
        return {}

    return captured, fake_post


def test_reduce_worker_results_writes_under_active_mem_ns(monkeypatch) -> None:
    mm = memory.MemoryManager(
        {"base_url": "http://x", "enabled": True, "agent_id": "ironclad"}
    )
    captured, fake_post = _make_capture()

    monkeypatch.setattr(mm, "_post", fake_post)
    monkeypatch.setattr(mm, "is_available", lambda: True)
    monkeypatch.setattr(gx10, "WORKER_WRITE", True)
    monkeypatch.setattr(gx10, "WORKER_WRITE_MODE", "reducer")
    monkeypatch.setattr(gx10, "_MEMORY", mm)

    ctx = ProjectContext("p", "/r", "feed1234beef5678")
    assert pc.current() is None
    with pc.use(ctx):
        n = gx10._reduce_worker_results(
            [{"ok": True, "content": "alpha"}, {"ok": True, "content": "beta"}],
            topic="parallel",
        )

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and not captured:
        time.sleep(0.02)

    assert n == 2
    assert captured, "fire-and-forget reducer add_bulk body was not captured"
    assert captured[0]["agent_id"] == "feed1234beef5678"
    assert pc.current() is None


def test_reduce_worker_results_default_partition_without_project(monkeypatch) -> None:
    mm = memory.MemoryManager(
        {"base_url": "http://x", "enabled": True, "agent_id": "ironclad"}
    )
    captured, fake_post = _make_capture()

    monkeypatch.setattr(mm, "_post", fake_post)
    monkeypatch.setattr(mm, "is_available", lambda: True)
    monkeypatch.setattr(gx10, "WORKER_WRITE", True)
    monkeypatch.setattr(gx10, "WORKER_WRITE_MODE", "reducer")
    monkeypatch.setattr(gx10, "_MEMORY", mm)

    assert pc.current() is None
    n = gx10._reduce_worker_results(
        [{"ok": True, "content": "alpha"}, {"ok": True, "content": "beta"}],
        topic="parallel",
    )

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and not captured:
        time.sleep(0.02)

    assert n == 2
    assert captured, "fire-and-forget reducer add_bulk body was not captured"
    assert captured[0]["agent_id"] == "ironclad"
    assert pc.current() is None
