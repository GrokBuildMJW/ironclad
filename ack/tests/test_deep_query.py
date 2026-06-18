"""MEM-10 / §3-Mechanismus 5 — opt-in deep_query_memory (graph path, off the hot path).

The hot read stays vector-only (graph=false); only deep_query opts into graph=true with a generous
timeout. Validates the MemoryManager method (graph flag + timeout + fail-soft) and the gx10 tool
(offered only when memory is configured + the run_tool handler).
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=object))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402
import memory  # noqa: E402
import pytest  # noqa: E402


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode("utf-8")
        self.status = 200

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def mm(monkeypatch):
    captured: dict = {}

    def _urlopen(req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        if url.endswith("/health"):
            return _Resp({"status": "ok"})
        if url.endswith("/search"):
            captured["search"] = json.loads(req.data.decode("utf-8"))
            captured["timeout"] = timeout
            return _Resp({"results": [{"memory": "X depends on Y"}, {"memory": "Y → Z"}]})
        return _Resp({})

    monkeypatch.setattr(memory.urllib.request, "urlopen", _urlopen)
    m = memory.MemoryManager({"base_url": "http://mem:8800", "agent_id": "ironclad", "deep_timeout": 40})
    m._captured = captured  # type: ignore[attr-defined]
    return m


# ── MemoryManager.deep_query ─────────────────────────────────────────────────
def test_deep_query_uses_graph_and_deep_timeout(mm):
    out = mm.deep_query("what depends on X", limit=3)
    assert "graph matches" in out and "X depends on Y" in out
    assert mm._captured["search"]["graph"] is True       # GRAPH path (not the vector hot path)
    assert mm._captured["search"]["limit"] == 3
    assert mm._captured["timeout"] == 40                 # generous deep timeout, not read_timeout


def test_hot_path_stays_vector_only(mm):
    mm.query("routine lookup")
    assert mm._captured["search"]["graph"] is False      # query_memory hot path unchanged


def test_deep_query_fail_soft_when_unconfigured():
    m = memory.MemoryManager({})                          # no endpoint
    assert m.deep_query("x") == "[Memory] no relational matches."


# ── gx10 tool registration + handler ─────────────────────────────────────────
class _FakeMem:
    def is_available(self):
        return True

    def deep_query(self, query, limit):
        return f"[Memory] graph matches:\n- dep({query}, {limit})"

    def query(self, query, limit):
        return "[Memory] matches:\n- vector"


@pytest.fixture(autouse=True)
def _restore_mem():
    prev = gx10._MEMORY
    yield
    gx10._MEMORY = prev


def test_deep_tool_offered_only_with_memory(monkeypatch):
    monkeypatch.setattr(gx10, "_MEMORY", None)
    assert all(t["function"]["name"] != "deep_query_memory" for t in gx10._effective_tools())
    monkeypatch.setattr(gx10, "_MEMORY", _FakeMem())
    names = [t["function"]["name"] for t in gx10._effective_tools()]
    assert "deep_query_memory" in names and "query_memory" in names  # both offered with memory


def test_handler_dispatches_to_deep_query(monkeypatch):
    monkeypatch.setattr(gx10, "_MEMORY", _FakeMem())
    out = gx10.run_tool("deep_query_memory", {"query": "what depends on X", "limit": 4})
    assert out == "[Memory] graph matches:\n- dep(what depends on X, 4)"


def test_handler_unavailable_without_memory(monkeypatch):
    monkeypatch.setattr(gx10, "_MEMORY", None)
    assert "unavailable" in gx10.run_tool("deep_query_memory", {"query": "x"})
