"""§3c MAP — fan-out workers as memory READ-citizens (workers.py + gx10._worker_contexts).

Validates without a live model / mem-api:

  * ``workers.fanout`` injects optional per-item context (prepended to each item); ``contexts=None``
    or a length mismatch ⇒ today's stateless behaviour (byte-identical).
  * ``gx10._worker_contexts`` returns per-item retrieved blocks when the flag is on, else None.
  * the ``parallel_reason`` handler forwards contexts only when the flag is on (off ⇒ None).
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
import workers  # noqa: E402
import pytest  # noqa: E402


# ── workers.fanout per-item context ──────────────────────────────────────────
class _Caps:
    def __init__(self):
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        user = [m for m in kw["messages"] if m["role"] == "user"][-1]["content"]
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=types.SimpleNamespace(content="ECHO:" + user))],
            usage=types.SimpleNamespace(completion_tokens=3))


class _FakeClient:
    def __init__(self):
        self.chat = types.SimpleNamespace(completions=_Caps())


def _workers():
    return workers.ReasoningWorkers(_FakeClient(), "m", max_concurrency=2,
                                    max_batch_tokens=1_000_000)


def test_fanout_injects_per_item_context():
    w = _workers()
    res = w.fanout(["item one", "item two"], system="INSTR", contexts=["CTX-A", "CTX-B"])
    assert res[0]["content"] == "ECHO:CTX-A\n\nitem one"   # context prepended, in order
    assert res[1]["content"] == "ECHO:CTX-B\n\nitem two"


def test_fanout_no_contexts_is_byte_identical():
    w = _workers()
    res = w.fanout(["a", "b"], system="I")
    assert res[0]["content"] == "ECHO:a" and res[1]["content"] == "ECHO:b"   # verbatim, no prefix


def test_fanout_length_mismatch_ignored():
    w = _workers()
    res = w.fanout(["a", "b"], contexts=["only one"])      # mismatch → fail safe to stateless
    assert res[0]["content"] == "ECHO:a" and res[1]["content"] == "ECHO:b"


def test_fanout_per_item_none_mixes():
    w = _workers()
    res = w.fanout(["a", "b"], contexts=["CTX", None])     # None for item 2 → that one verbatim
    assert res[0]["content"] == "ECHO:CTX\n\na"
    assert res[1]["content"] == "ECHO:b"


# ── gx10._worker_contexts ────────────────────────────────────────────────────
class _FakeMem:
    def __init__(self, hits, available=True):
        self._hits = list(hits)
        self._available = available

    def is_available(self):
        return self._available

    def search(self, query, limit):
        return list(self._hits)[:limit]


def test_worker_contexts_off_returns_none(monkeypatch):
    monkeypatch.setattr(gx10, "WORKER_MEMORY", False)
    monkeypatch.setattr(gx10, "_MEMORY", _FakeMem(["x"]))
    assert gx10._worker_contexts(["a", "b"]) is None


def test_worker_contexts_on_builds_per_item(monkeypatch):
    monkeypatch.setattr(gx10, "WORKER_MEMORY", True)
    monkeypatch.setattr(gx10, "_MEMORY", _FakeMem(["fact one", "fact two"]))
    monkeypatch.setattr(gx10, "_WARM", None)
    ctx = gx10._worker_contexts(["q1", "q2"])
    assert ctx is not None and len(ctx) == 2
    assert all(c.startswith(gx10._RAG_MARKER) for c in ctx)
    assert "fact one" in ctx[0]


def test_worker_contexts_no_hits_returns_none(monkeypatch):
    monkeypatch.setattr(gx10, "WORKER_MEMORY", True)
    monkeypatch.setattr(gx10, "_MEMORY", _FakeMem([]))     # no hits for any item
    monkeypatch.setattr(gx10, "_WARM", None)
    assert gx10._worker_contexts(["q1", "q2"]) is None


def test_worker_contexts_unavailable_returns_none(monkeypatch):
    monkeypatch.setattr(gx10, "WORKER_MEMORY", True)
    monkeypatch.setattr(gx10, "_MEMORY", _FakeMem(["x"], available=False))
    assert gx10._worker_contexts(["q1"]) is None
    monkeypatch.setattr(gx10, "_MEMORY", None)
    assert gx10._worker_contexts(["q1"]) is None


# ── parallel_reason wiring ───────────────────────────────────────────────────
class _FakeWorkers:
    def __init__(self):
        self.last = None

    def fanout(self, items, *, system=None, contexts=None, max_tokens=None, think=True):
        self.last = {"items": list(items), "system": system, "contexts": contexts}
        return [{"ok": True, "content": "r", "error": None} for _ in items]


@pytest.fixture(autouse=True)
def _restore(monkeypatch):
    prev = gx10._WORKERS
    yield
    gx10._WORKERS = prev


def test_parallel_reason_forwards_contexts_when_on(monkeypatch):
    fw = _FakeWorkers()
    gx10._WORKERS = fw
    monkeypatch.setattr(gx10, "WORKER_MEMORY", True)
    monkeypatch.setattr(gx10, "_MEMORY", _FakeMem(["fact"]))
    monkeypatch.setattr(gx10, "_WARM", None)
    gx10.run_tool("parallel_reason", {"items": ["q1", "q2"], "instruction": "INSTR"})
    assert fw.last["contexts"] is not None and len(fw.last["contexts"]) == 2
    assert "fact" in fw.last["contexts"][0]
    assert fw.last["system"] == "INSTR"


def test_parallel_reason_no_contexts_when_off(monkeypatch):
    fw = _FakeWorkers()
    gx10._WORKERS = fw
    monkeypatch.setattr(gx10, "WORKER_MEMORY", False)
    gx10.run_tool("parallel_reason", {"items": ["q1"], "instruction": "I"})
    assert fw.last["contexts"] is None        # byte-identical stateless fan-out
