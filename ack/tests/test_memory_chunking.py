"""B3 — long-artifact chunking + recency ranking (engine/memory.py).

Validates against a stubbed HTTP service (no live mem-api):

  * ``_chunks`` covers the whole text with bounded, overlapping passages.
  * ``chunk_and_store`` posts every chunk via ``/add_bulk`` (vector-only), preserving a phrase that
    a 4000-char truncation would have dropped; no-op when disabled.
  * ``store_task_completion`` flag OFF ⇒ byte-identical (episode truncated, NO chunk writes); flag ON
    ⇒ long feedback additionally chunk-stored losslessly; short feedback not chunked.
  * ``_search`` recency tiebreak OFF ⇒ server order; ON ⇒ score desc, ties by ``created_at`` desc.
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


def _fake(captured):
    def _urlopen(req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        body = None
        if not isinstance(req, str) and req.data:
            body = json.loads(req.data.decode("utf-8"))
        if url.endswith("/health"):
            return _Resp({"status": "ok"})
        if url.endswith("/add_bulk"):
            captured.setdefault("bulk", []).append(body)
            return _Resp({"results": []})
        if url.endswith("/add"):
            captured.setdefault("add", []).append(body)
            return _Resp({"results": []})
        if url.endswith("/search"):
            captured.setdefault("search", []).append(body)
            return _Resp({"results": captured.get("search_results", [])})
        return _Resp({})
    return _urlopen


def _wait(pred, tries=80, delay=0.05):
    for _ in range(tries):
        if pred():
            return True
        time.sleep(delay)
    return pred()


# ── _chunks ──────────────────────────────────────────────────────────────────
def test_chunks_cover_overlap_and_bound():
    text = "A" * 19000 + "ZULU-LAST" + "C" * 900           # ~19909 chars, marker near the end
    chunks = memory.MemoryManager._chunks(text, 6000, 400)
    assert len(chunks) >= 3
    assert all(len(c) <= 6000 for c in chunks)             # bounded
    assert chunks[0][-400:] == chunks[1][:400]             # consecutive overlap (step = size-overlap)
    assert any("ZULU-LAST" in c for c in chunks)           # end is covered (no truncation)
    # full coverage: every char index lands in some chunk (last chunk reaches the end)
    assert chunks[-1].endswith("C" * 900)


def test_chunks_edge_cases():
    assert memory.MemoryManager._chunks("", 6000, 400) == []
    assert memory.MemoryManager._chunks("   ", 6000, 400) == []
    assert memory.MemoryManager._chunks("abc", 0, 0) == ["abc"]   # size<=0 → whole text


# ── chunk_and_store ────────────────────────────────────────────────────────────
def test_chunk_and_store_posts_every_chunk(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(memory.urllib.request, "urlopen", _fake(captured))
    m = memory.MemoryManager({"base_url": "http://mem:8800", "agent_id": "ironclad",
                              "chunk_size": 6000, "chunk_overlap": 400})
    text = "A" * 19000 + "ZULU-LAST" + "C" * 900
    expected = len(memory.MemoryManager._chunks(text, 6000, 400))
    m.chunk_and_store(text, {"task_id": "KGC-1"}, source="task_completion")
    assert _wait(lambda: len(captured.get("bulk", [])) >= expected)
    bulk = captured["bulk"]
    assert len(bulk) == expected
    assert all(b["infer"] is False for b in bulk)                       # vector-only
    assert bulk[0]["metadata"]["chunk"] == 0 and bulk[0]["metadata"]["chunks"] == expected
    assert bulk[0]["metadata"]["task_id"] == "KGC-1"
    assert bulk[0]["metadata"]["source"] == "task_completion"
    assert bulk[0]["agent_id"] == "ironclad"
    assert any("ZULU-LAST" in b["messages"][0]["content"] for b in bulk)  # not truncated away


def test_chunk_and_store_noop_when_disabled():
    m = memory.MemoryManager({})
    m.chunk_and_store("x" * 20000, {"task_id": "KGC-9"})   # must not raise, no transport


# ── store_task_completion (flag-gated) ──────────────────────────────────────────
def test_store_flag_off_is_byte_identical(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(memory.urllib.request, "urlopen", _fake(captured))
    m = memory.MemoryManager({"base_url": "http://mem:8800", "agent_id": "ironclad",
                              "chunk_long_artifacts": False})   # explicit OFF (default is now ON)
    m.store_task_completion("KGC-2", {"type": "f", "title": "t"}, "Z" * 10000)
    assert _wait(lambda: bool(captured.get("add")))
    time.sleep(0.2)                                         # give any stray chunk thread a chance
    assert len(captured["add"]) == 1
    assert "bulk" not in captured                           # NO chunking when flag off
    assert captured["add"][0]["messages"][0]["content"].count("Z") == 4000   # episode still truncated


def test_store_flag_on_chunks_long_feedback(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(memory.urllib.request, "urlopen", _fake(captured))
    m = memory.MemoryManager({"base_url": "http://mem:8800", "agent_id": "ironclad",
                              "chunk_long_artifacts": True, "chunk_size": 6000})
    m.store_task_completion("KGC-3", {"type": "f", "title": "t"}, "Y" * 10000 + "TANGO")
    assert _wait(lambda: captured.get("add") and len(captured.get("bulk", [])) >= 2)
    assert len(captured["add"]) == 1                        # episode (inferred /add) unchanged
    assert any("TANGO" in b["messages"][0]["content"] for b in captured["bulk"])  # full text preserved
    assert captured["bulk"][0]["metadata"]["task_id"] == "KGC-3"
    assert captured["bulk"][0]["metadata"]["source"] == "task_completion"


def test_store_flag_on_short_feedback_not_chunked(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(memory.urllib.request, "urlopen", _fake(captured))
    m = memory.MemoryManager({"base_url": "http://mem:8800", "agent_id": "ironclad",
                              "chunk_long_artifacts": True})
    m.store_task_completion("KGC-4", {"type": "f"}, "short feedback")
    assert _wait(lambda: bool(captured.get("add")))
    time.sleep(0.2)
    assert "bulk" not in captured                           # ≤ cap → already in the episode, no chunks


# ── _search recency tiebreak (flag-gated) ──────────────────────────────────────
_SERVER_RESULTS = [
    {"memory": "old hi-score", "score": 0.9, "created_at": "2024-01-01"},
    {"memory": "new hi-score", "score": 0.9, "created_at": "2024-06-01"},
    {"memory": "lo-score",     "score": 0.5, "created_at": "2025-12-01"},
]


def test_search_recency_off_keeps_server_order(monkeypatch):
    captured = {"search_results": list(_SERVER_RESULTS)}
    monkeypatch.setattr(memory.urllib.request, "urlopen", _fake(captured))
    m = memory.MemoryManager({"base_url": "http://mem:8800", "agent_id": "ironclad",
                              "recency_tiebreak": False})        # explicit OFF (default is now ON)
    assert m.search("q", 5) == ["old hi-score", "new hi-score", "lo-score"]


def test_defaults_are_on():
    # 06-18 decision: the memory techniques are active by DEFAULT (no flag needed).
    m = memory.MemoryManager({"base_url": "http://mem:8800"})
    assert m.chunk_long is True and m.recency_tiebreak is True


def test_search_recency_on_breaks_ties_by_created_at(monkeypatch):
    captured = {"search_results": list(_SERVER_RESULTS)}
    monkeypatch.setattr(memory.urllib.request, "urlopen", _fake(captured))
    m = memory.MemoryManager({"base_url": "http://mem:8800", "agent_id": "ironclad",
                              "recency_tiebreak": True})
    # score desc keeps the 0.9 pair first; within the tie the more recent (2024-06) wins
    assert m.search("q", 5) == ["new hi-score", "old hi-score", "lo-score"]
