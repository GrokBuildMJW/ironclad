"""Optional memory backend (engine/memory.py).

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


def test_search_respects_enabled_flag():
    # MEM-2 (#503): _search must gate on `enabled` (like every write + the other reads) — a base set but
    # enabled=false must NOT issue /search. Returns [] without touching the network (no stub needed).
    m = memory.MemoryManager({"base_url": "http://mem:8800", "enabled": False})
    assert m.base and m.enabled is False          # base present, but disabled
    assert m._search("anything", 5) == []         # gated off, no /search


# ── #458 (D1): richer token-budgeted handover brief ───────────────────────────────────────────────
def _count4(s):
    return len(s or "") // 4 + 1


def test_brief_composes_warm_summary_vector_and_relational(mm, monkeypatch):
    def fake(q, limit, graph=False, timeout=None):
        if graph:
            return ["rel: A depends on B", "past decision X"]   # 2nd dups a vector hit → must dedup
        return ["past decision X", "gotcha Y"]
    monkeypatch.setattr(mm, "_search", fake)
    out = mm.brief(body="wire the failover", title="router", warm_summary="We chose SOFT anti-affinity.",
                   budget_tokens=1000, count_tokens=_count4)
    assert out.startswith("## Relevant context from memory")
    assert "### Recent context (rolling summary)" in out and "We chose SOFT anti-affinity." in out
    assert "### Related past work" in out and "- past decision X" in out and "- gotcha Y" in out
    assert "### Connections (relational)" in out and "- rel: A depends on B" in out
    assert out.count("past decision X") == 1                    # the relational dup was removed


def test_brief_is_token_budgeted(mm, monkeypatch):
    monkeypatch.setattr(mm, "_search", lambda *a, **k: ["hit one", "hit two", "hit three"])
    # budget = the warm section as RENDERED (head + "\n"+heading + "\n"+line), so it fits exactly and the
    # next section's heading pushes over → dropped (newline-aware accounting, review B S3-1).
    budget = (_count4("## Relevant context from memory")
              + _count4("\n### Recent context (rolling summary)")
              + _count4("\nROLLING"))
    out = mm.brief(body="b", title="t", warm_summary="ROLLING", deep=False,
                   budget_tokens=budget, count_tokens=_count4)
    assert "ROLLING" in out and "### Related past work" not in out   # only what fits within budget survives


def test_brief_deep_off_skips_relational(mm, monkeypatch):
    calls = {"graph": 0}
    def fake(q, limit, graph=False, timeout=None):
        if graph:
            calls["graph"] += 1
        return ["v1", "v2"]
    monkeypatch.setattr(mm, "_search", fake)
    out = mm.brief(body="b", title="t", deep=False, budget_tokens=1000, count_tokens=_count4)
    assert calls["graph"] == 0 and "### Connections" not in out


def test_brief_is_body_keyed(mm):
    # the handover BODY (not just type:title) must reach the vector query → richer retrieval
    mm.brief(body="implement the circuit breaker for budget exhaustion", title="breaker",
             deep=False, budget_tokens=1000, count_tokens=_count4)
    assert "circuit breaker for budget exhaustion" in mm._captured["search"]["query"]


def test_brief_empty_when_nothing(mm, monkeypatch):
    monkeypatch.setattr(mm, "_search", lambda *a, **k: [])
    assert mm.brief(body="x", title="t", warm_summary="", count_tokens=_count4) == ""


def test_brief_is_fail_soft(mm, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("mem down")
    monkeypatch.setattr(mm, "_search", boom)
    assert mm.brief(body="x", title="t", warm_summary="", count_tokens=_count4) == ""   # never raises


def test_brief_warm_summary_survives_a_search_error(mm, monkeypatch):
    # a vector hiccup must not discard a warm summary already in hand (#458 D1 robustness)
    def boom(*a, **k):
        raise RuntimeError("mem down")
    monkeypatch.setattr(mm, "_search", boom)
    out = mm.brief(body="x", title="t", warm_summary="the rolling summary", budget_tokens=1000, count_tokens=_count4)
    assert "the rolling summary" in out and "### Related past work" not in out


def test_brief_relational_query_timeout_is_capped(mm, monkeypatch):
    # review A S3-1: the brief's graph enrichment must NOT use the full deep_timeout (40s) — a slow graph
    # store would otherwise stall a handover stage. Assert the graph call gets the short brief timeout.
    mm.deep_timeout = 40.0
    seen_to = {}
    def fake(q, limit, graph=False, timeout=None):
        if graph:
            seen_to["t"] = timeout
            return []
        return ["v1"]
    monkeypatch.setattr(mm, "_search", fake)
    mm.brief(body="b", title="t", deep=True, budget_tokens=1000, count_tokens=_count4)
    assert seen_to["t"] == mm._BRIEF_DEEP_TIMEOUT and seen_to["t"] < mm.deep_timeout


def test_brief_budget_accounts_for_newline_separators(mm, monkeypatch):
    # review B S3-1: with a tokenizer that charges for newlines, the RENDERED brief (after "\n".join)
    # must still fit budget — the accounting counts "\n"+piece, so summed-without-separators can't overflow.
    monkeypatch.setattr(mm, "_search", lambda q, l, graph=False, timeout=None:
                        [] if graph else ["aaaa", "bbbb", "cccc", "dddd", "eeee"])
    chars = lambda s: len(s or "")              # 1 token per char, INCLUDING newlines
    budget = 80
    out = mm.brief(body="b", title="t", deep=False, budget_tokens=budget, count_tokens=chars)
    assert out and chars(out) <= budget         # rendered length (with newlines) never exceeds budget


def test_brief_skips_relational_when_deep_timeout_disabled(mm, monkeypatch):
    # review B S3-2: deep_timeout<=0 means relational disabled — the brief must NOT issue a graph search
    # (and must never pass 0 to urlopen, which read_timeout-coalescing used to mask).
    mm.deep_timeout = 0.0
    calls = {"graph": 0}
    def fake(q, l, graph=False, timeout=None):
        if graph:
            calls["graph"] += 1
        return ["v1"]
    monkeypatch.setattr(mm, "_search", fake)
    out = mm.brief(body="b", title="t", deep=True, budget_tokens=1000, count_tokens=_count4)
    assert calls["graph"] == 0 and "### Connections" not in out


def test_search_honors_explicit_zero_timeout(mm, monkeypatch):
    # review B S3-2 root cause: _search must pass an explicit timeout through (is-None check), not coalesce
    # a falsy 0 to read_timeout. Existing None callers still get read_timeout.
    seen = {}
    monkeypatch.setattr(mm, "_post", lambda path, body, timeout: (seen.update(t=timeout), {"results": []})[1])
    mm._search("q", 3, graph=True, timeout=0.0)
    assert seen["t"] == 0.0
    mm._search("q", 3)                           # default None → read_timeout (back-compat)
    assert seen["t"] == mm.read_timeout
