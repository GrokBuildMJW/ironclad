"""Optional memory backend (core/engine/memory.py).

Validates the MemoryManager contract against a stubbed HTTP service: health/availability,
vector-only search (graph=false), result formatting, fire-and-forget store, and the
fully-disabled (no endpoint) path. No live mem-api needed.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import memory  # noqa: E402
import pytest  # noqa: E402


class _Resp:
    def __init__(self, payload, status=200):
        self._b = json.dumps(payload).encode("utf-8")
        self.status = status

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_urlopen(captured):
    def _urlopen(req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        body = None
        if not isinstance(req, str) and req.data:
            body = json.loads(req.data.decode("utf-8"))
        if url.endswith("/health"):
            return _Resp({"status": "ok"})
        if url.endswith("/search"):
            captured["search"] = body
            return _Resp({"results": [{"memory": "past decision X"},
                                      {"memory": "gotcha Y"}]})
        if url.endswith("/add"):
            captured["add"] = body
            return _Resp({"results": []})
        return _Resp({})
    return _urlopen


@pytest.fixture
def mm(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(memory.urllib.request, "urlopen", _fake_urlopen(captured))
    m = memory.MemoryManager({"base_url": "http://mem:8800", "agent_id": "ironclad"})
    m._captured = captured  # type: ignore[attr-defined]
    return m


def test_available(mm):
    assert mm.is_available() is True


def test_search_is_vector_only_and_formats(mm):
    out = mm.query("how did we do X?", limit=5)
    assert "past decision X" in out and "gotcha Y" in out
    # read path MUST send graph=false (graph store times out)
    assert mm._captured["search"]["graph"] is False
    assert mm._captured["search"]["agent_id"] == "ironclad"
    assert mm._captured["search"]["limit"] == 5


def test_get_context_formats_or_empty(mm):
    ctx = mm.get_context("backend", "Add rate limiting")
    assert ctx.startswith("## Relevant context") and "past decision X" in ctx


def test_store_is_fire_and_forget(mm):
    mm.store_task_completion("KGC-9", {"type": "feature", "title": "wire memory",
                                       "description": "do it"}, "all green")
    for _ in range(40):  # the POST runs in a daemon thread
        if "add" in mm._captured:
            break
        time.sleep(0.05)
    add = mm._captured.get("add")
    assert add and add["metadata"]["task_id"] == "KGC-9"
    assert "KGC-9" in add["messages"][0]["content"]
    assert add["agent_id"] == "ironclad"


def test_disabled_when_no_endpoint():
    m = memory.MemoryManager({})
    assert m.is_available() is False
    assert m.query("anything") == "[Memory] no relevant matches."
    m.store_task_completion("KGC-1", {}, "x")  # must not raise
