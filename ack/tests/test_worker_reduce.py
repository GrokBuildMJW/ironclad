"""§3c REDUCE — single-writer cold consolidation + shared rolling-summary floor (gx10.py).

Validates without a live model / mem-api / Valkey:

  * ``_reduce_worker_results``: OFF ⇒ no write (byte-identical); ON (reducer) ⇒ ONE consolidated
    cold write of the deduped OK outputs; ``direct`` mode steps back; large blob → chunk_and_store;
    fail-soft on unavailable / unconfigured.
  * ``_worker_shared_floor``: the shared summary from the warm tier (or "" when off / no warm / none).
  * ``parallel_reason`` folds the floor into ``system`` and runs the reducer — only when flagged on.
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


class _FakeMem:
    def __init__(self, available=True, chunk_size=6000):
        self._available = available
        self.chunk_size = chunk_size
        self.bulk = []
        self.chunks = []

    def is_available(self):
        return self._available

    def search(self, q, k):
        return []

    def add_bulk(self, text, metadata=None):
        self.bulk.append((text, metadata))

    def chunk_and_store(self, text, metadata=None, *, source="artifact"):
        self.chunks.append((text, metadata, source))


class _FakeWarm:
    def __init__(self, summary=None):
        self._summary = summary
        self.sets = []

    def get_session(self, sid, field):
        return self._summary if field == "summary" else None

    def set_session(self, sid, field, value, ttl=None):
        self.sets.append((sid, field, value))
        return True


class _FakeWorkers:
    def __init__(self):
        self.last = None

    def fanout(self, items, *, system=None, contexts=None, max_tokens=None, think=True):
        self.last = {"items": list(items), "system": system, "contexts": contexts}
        return [{"ok": True, "content": "r", "error": None} for _ in items]


@pytest.fixture(autouse=True)
def _restore():
    prev = gx10._WORKERS
    yield
    gx10._WORKERS = prev


# ── _reduce_worker_results ───────────────────────────────────────────────────
def test_reduce_off_writes_nothing(monkeypatch):
    monkeypatch.setattr(gx10, "WORKER_WRITE", False)
    mem = _FakeMem()
    monkeypatch.setattr(gx10, "_MEMORY", mem)
    assert gx10._reduce_worker_results([{"ok": True, "content": "x"}], topic="t") == 0
    assert mem.bulk == [] and mem.chunks == []


def test_reduce_on_one_consolidated_write(monkeypatch):
    monkeypatch.setattr(gx10, "WORKER_WRITE", True)
    monkeypatch.setattr(gx10, "WORKER_WRITE_MODE", "reducer")
    mem = _FakeMem()
    monkeypatch.setattr(gx10, "_MEMORY", mem)
    res = [{"ok": True, "content": "alpha"},
           {"ok": False, "content": None, "error": "boom"},   # skipped
           {"ok": True, "content": "beta"}]
    n = gx10._reduce_worker_results(res, topic="my topic")
    assert n == 2
    assert len(mem.bulk) == 1 and mem.chunks == []            # ONE write, no per-worker races
    text, md = mem.bulk[0]
    assert "alpha" in text and "beta" in text and "my topic" in text
    assert md["source"] == "worker_reduce" and md["topic"] == "my topic"


def test_reduce_dedups_identical_outputs(monkeypatch):
    monkeypatch.setattr(gx10, "WORKER_WRITE", True)
    monkeypatch.setattr(gx10, "WORKER_WRITE_MODE", "reducer")
    mem = _FakeMem()
    monkeypatch.setattr(gx10, "_MEMORY", mem)
    res = [{"ok": True, "content": "same answer"}, {"ok": True, "content": "same answer"}]
    assert gx10._reduce_worker_results(res) == 1
    assert len(mem.bulk) == 1


def test_reduce_direct_mode_steps_back(monkeypatch):
    monkeypatch.setattr(gx10, "WORKER_WRITE", True)
    monkeypatch.setattr(gx10, "WORKER_WRITE_MODE", "direct")
    mem = _FakeMem()
    monkeypatch.setattr(gx10, "_MEMORY", mem)
    assert gx10._reduce_worker_results([{"ok": True, "content": "x"}]) == 0
    assert mem.bulk == [] and mem.chunks == []


def test_reduce_large_blob_is_chunked(monkeypatch):
    monkeypatch.setattr(gx10, "WORKER_WRITE", True)
    monkeypatch.setattr(gx10, "WORKER_WRITE_MODE", "reducer")
    mem = _FakeMem(chunk_size=50)                              # tiny cap → blob exceeds it
    monkeypatch.setattr(gx10, "_MEMORY", mem)
    n = gx10._reduce_worker_results([{"ok": True, "content": "Z" * 200}], topic="t")
    assert n == 1
    assert mem.chunks and not mem.bulk                        # routed to chunk_and_store (B3)
    assert mem.chunks[0][2] == "worker_reduce"


def test_reduce_fail_soft(monkeypatch):
    monkeypatch.setattr(gx10, "WORKER_WRITE", True)
    monkeypatch.setattr(gx10, "WORKER_WRITE_MODE", "reducer")
    monkeypatch.setattr(gx10, "_MEMORY", _FakeMem(available=False))
    assert gx10._reduce_worker_results([{"ok": True, "content": "x"}]) == 0
    monkeypatch.setattr(gx10, "_MEMORY", None)
    assert gx10._reduce_worker_results([{"ok": True, "content": "x"}]) == 0


# ── _worker_shared_floor ─────────────────────────────────────────────────────
def test_floor_off_is_empty(monkeypatch):
    monkeypatch.setattr(gx10, "WORKER_MEMORY", False)
    monkeypatch.setattr(gx10, "_WARM", _FakeWarm("rolling state"))
    assert gx10._worker_shared_floor() == ""


def test_floor_on_returns_summary_block(monkeypatch):
    monkeypatch.setattr(gx10, "WORKER_MEMORY", True)
    monkeypatch.setattr(gx10, "_WARM", _FakeWarm("rolling state here"))
    f = gx10._worker_shared_floor()
    assert f.startswith(gx10._SUMMARY_MARKER) and "rolling state here" in f


def test_floor_no_warm_or_no_summary(monkeypatch):
    monkeypatch.setattr(gx10, "WORKER_MEMORY", True)
    monkeypatch.setattr(gx10, "_WARM", None)
    assert gx10._worker_shared_floor() == ""
    monkeypatch.setattr(gx10, "_WARM", _FakeWarm(None))
    assert gx10._worker_shared_floor() == ""


# ── parallel_reason wiring ───────────────────────────────────────────────────
def test_parallel_reason_folds_floor_and_reduces(monkeypatch):
    fw = _FakeWorkers()
    gx10._WORKERS = fw
    monkeypatch.setattr(gx10, "WORKER_MEMORY", True)
    monkeypatch.setattr(gx10, "WORKER_WRITE", True)
    monkeypatch.setattr(gx10, "WORKER_WRITE_MODE", "reducer")
    monkeypatch.setattr(gx10, "_WARM", _FakeWarm("shared summary state"))
    mem = _FakeMem()
    monkeypatch.setattr(gx10, "_MEMORY", mem)
    gx10.run_tool("parallel_reason", {"items": ["q1", "q2"], "instruction": "INSTR"})
    assert "shared summary state" in fw.last["system"] and "INSTR" in fw.last["system"]
    assert len(mem.bulk) == 1                                  # reducer consolidated the outputs once


def test_parallel_reason_off_keeps_system_and_skips_reduce(monkeypatch):
    fw = _FakeWorkers()
    gx10._WORKERS = fw
    monkeypatch.setattr(gx10, "WORKER_MEMORY", False)
    monkeypatch.setattr(gx10, "WORKER_WRITE", False)
    mem = _FakeMem()
    monkeypatch.setattr(gx10, "_MEMORY", mem)
    gx10.run_tool("parallel_reason", {"items": ["q1"], "instruction": "INSTR"})
    assert fw.last["system"] == "INSTR"                        # no floor → byte-identical
    assert fw.last["contexts"] is None
    assert mem.bulk == [] and mem.chunks == []                # no reduce write
