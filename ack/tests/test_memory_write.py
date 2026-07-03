"""#1076 (epic #1043 quick-win): the deliberate `remember` memory-write tool.

The model can now PERSIST a durable fact/decision into project memory (fire-and-forget via
MemoryManager.add_bulk, scope-aware) so it survives the session and is retrieved later via query_memory /
RAG — the write counterpart to query_memory / deep_query_memory. Offered only when a memory store is
configured.
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


class _FakeMem:
    def __init__(self):
        self.calls = []

    def is_available(self):
        return True

    def add_bulk(self, text, metadata=None):
        self.calls.append((text, metadata))


def test_remember_registered_only_when_memory_configured(monkeypatch):
    monkeypatch.setattr(gx10, "_MEMORY", _FakeMem())
    assert "remember" in {t["function"]["name"] for t in gx10._effective_tools()}
    monkeypatch.setattr(gx10, "_MEMORY", None)
    assert "remember" not in {t["function"]["name"] for t in gx10._effective_tools()}


def test_remember_persists_via_add_bulk(monkeypatch):
    fm = _FakeMem()
    monkeypatch.setattr(gx10, "_MEMORY", fm)
    out = gx10.run_tool("remember", {"text": "Use write_last_reply for large files on small models"})
    assert out.startswith("OK: remembered")
    assert fm.calls and fm.calls[0][0] == "Use write_last_reply for large files on small models"
    assert fm.calls[0][1].get("source") == "model_remember"


def test_remember_unavailable_when_no_store(monkeypatch):
    monkeypatch.setattr(gx10, "_MEMORY", None)
    assert gx10.run_tool("remember", {"text": "x"}).startswith("[Memory] unavailable")


def test_remember_rejects_empty_text(monkeypatch):
    fm = _FakeMem()
    monkeypatch.setattr(gx10, "_MEMORY", fm)
    assert gx10.run_tool("remember", {"text": "   "}).startswith("ERROR: remember needs")
    assert fm.calls == []                                       # nothing written on an empty note
