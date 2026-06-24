"""B2 — per-turn auto-retrieval assembly (engine/gx10.py + memory.search).

Locks B2 WITHOUT a live model / mem-api / Valkey:

  * **Flag OFF ⇒ byte-identical.** ``_retrieve_context`` returns "" with zero memory touch, and
    ``run()`` appends the user message verbatim (the plan's hard requirement).
  * **Flag ON** retrieves vector-only, dedups against the live window, token-budgets the block,
    and ``run()`` prepends ``## Relevant context (retrieved)`` to the user message.
  * **Cache-aside**: a warm-tier hit skips the cold store; a miss populates it.
  * **Fail-soft**: a search error / unavailable / unconfigured memory all degrade to "".
"""
from __future__ import annotations

import sys
from pathlib import Path

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402
import memory  # noqa: E402


# ── memory.search (public vector wrapper) ────────────────────────────────────
def test_memory_search_is_vector_only(monkeypatch):
    import json as _json
    captured: dict = {}

    class _R:
        def __init__(self, p):
            self._b = _json.dumps(p).encode("utf-8")
            self.status = 200

        def read(self):
            return self._b

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _urlopen(req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        if url.endswith("/search"):
            captured["body"] = _json.loads(req.data.decode("utf-8"))
            return _R({"results": [{"memory": "m1"}, {"memory": "m2"}]})
        return _R({})

    monkeypatch.setattr(memory.urllib.request, "urlopen", _urlopen)
    m = memory.MemoryManager({"base_url": "http://mem:8800", "agent_id": "ironclad"})
    hits = m.search("q", 7)
    assert hits == ["m1", "m2"]
    assert captured["body"]["graph"] is False        # vector-only
    assert captured["body"]["limit"] == 7


# ── _retrieve_context / run() ────────────────────────────────────────────────
class _FakeMem:
    def __init__(self, hits, available=True, boom=False):
        self._hits = list(hits)
        self._available = available
        self._boom = boom
        self.search_calls = 0

    def is_available(self):
        return self._available

    def search(self, query, limit):
        self.search_calls += 1
        if self._boom:
            raise RuntimeError("mem down")
        return list(self._hits)[:limit]


class _FakeWarm:
    def __init__(self, cached=None):
        self._cached = cached
        self.gets = 0
        self.sets = []

    def cache_get(self, q):
        self.gets += 1
        return self._cached

    def cache_set(self, q, results, ttl=None):
        self.sets.append((q, list(results)))
        return True


def _mk_agent(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(gx10, "OpenAI", lambda **kw: object())
    g = gx10.GX10(base_url="http://x/v1", api_key="k", model="m", prompt_path="")
    g.messages = [{"role": "system", "content": "SYS"}]
    return g


def test_flag_off_is_noop_and_touches_no_memory(monkeypatch, tmp_path):
    monkeypatch.setattr(gx10, "RAG_ENABLED", False)
    mem = _FakeMem(["x"])
    monkeypatch.setattr(gx10, "_MEMORY", mem)
    g = _mk_agent(monkeypatch, tmp_path)
    assert g._retrieve_context("anything") == ""
    assert mem.search_calls == 0                       # no retrieval, no network


def test_flag_on_builds_marked_block(monkeypatch, tmp_path):
    monkeypatch.setattr(gx10, "RAG_ENABLED", True)
    mem = _FakeMem(["alpha fact", "beta fact"])
    monkeypatch.setattr(gx10, "_MEMORY", mem)
    monkeypatch.setattr(gx10, "_WARM", None)
    g = _mk_agent(monkeypatch, tmp_path)
    out = g._retrieve_context("q")
    assert out.startswith(gx10._RAG_MARKER)
    assert "alpha fact" in out and "beta fact" in out
    assert mem.search_calls == 1


def test_dedups_against_live_window(monkeypatch, tmp_path):
    monkeypatch.setattr(gx10, "RAG_ENABLED", True)
    monkeypatch.setattr(gx10, "_MEMORY", _FakeMem(["already here", "new fact"]))
    monkeypatch.setattr(gx10, "_WARM", None)
    g = _mk_agent(monkeypatch, tmp_path)
    g.messages = [{"role": "system", "content": "SYS"},
                  {"role": "assistant", "content": "we noted: already here, indeed"}]
    out = g._retrieve_context("q")
    assert "new fact" in out
    assert "already here" not in out                   # in-window → not re-injected


def test_token_budget_caps_lines(monkeypatch, tmp_path):
    monkeypatch.setattr(gx10, "RAG_ENABLED", True)
    # #366: the RAG budget is now enforced in REAL tokens (calibrated fallback here, no live
    # tokenizer). A 30-char hit ≈ ceil(33/2.6)=13 tokens; budget 20 fits one, not two.
    monkeypatch.setattr(gx10, "RAG_MAX_TOKENS", 20)
    monkeypatch.setattr(gx10, "_MEMORY", _FakeMem(["a" * 30, "b" * 30]))
    monkeypatch.setattr(gx10, "_WARM", None)
    g = _mk_agent(monkeypatch, tmp_path)
    out = g._retrieve_context("q")
    assert ("a" * 30 in out) and ("b" * 30 not in out)            # only the first hit fits
    assert out.count("- ") == 1


def test_cache_hit_skips_cold_store(monkeypatch, tmp_path):
    monkeypatch.setattr(gx10, "RAG_ENABLED", True)
    mem = _FakeMem(["should not be read"])
    warm = _FakeWarm(cached=["cached fact"])
    monkeypatch.setattr(gx10, "_MEMORY", mem)
    monkeypatch.setattr(gx10, "_WARM", warm)
    g = _mk_agent(monkeypatch, tmp_path)
    out = g._retrieve_context("q")
    assert "cached fact" in out
    assert mem.search_calls == 0                        # cache-aside hit → cold skipped
    assert warm.gets == 1


def test_cache_miss_populates_warm(monkeypatch, tmp_path):
    monkeypatch.setattr(gx10, "RAG_ENABLED", True)
    mem = _FakeMem(["fresh fact"])
    warm = _FakeWarm(cached=None)
    monkeypatch.setattr(gx10, "_MEMORY", mem)
    monkeypatch.setattr(gx10, "_WARM", warm)
    g = _mk_agent(monkeypatch, tmp_path)
    out = g._retrieve_context("q")
    assert "fresh fact" in out
    assert mem.search_calls == 1
    assert warm.sets and warm.sets[0][1] == ["fresh fact"]


def test_fail_soft_paths(monkeypatch, tmp_path):
    g = _mk_agent(monkeypatch, tmp_path)
    monkeypatch.setattr(gx10, "RAG_ENABLED", True)
    # search raises
    monkeypatch.setattr(gx10, "_WARM", None)
    monkeypatch.setattr(gx10, "_MEMORY", _FakeMem([], boom=True))
    assert g._retrieve_context("q") == ""
    # memory unavailable
    mem = _FakeMem(["x"], available=False)
    monkeypatch.setattr(gx10, "_MEMORY", mem)
    assert g._retrieve_context("q") == ""
    assert mem.search_calls == 0
    # memory unconfigured
    monkeypatch.setattr(gx10, "_MEMORY", None)
    assert g._retrieve_context("q") == ""


def test_run_flag_off_appends_user_verbatim(monkeypatch, tmp_path):
    monkeypatch.setattr(gx10, "RAG_ENABLED", False)
    g = _mk_agent(monkeypatch, tmp_path)
    g._generate = lambda think: ("ok", [], False, None, None)   # end the loop in one iteration
    g.run("hello world")
    last_user = [m for m in g.messages if m.get("role") == "user"][-1]
    assert last_user["content"] == "hello world"                # byte-identical


def test_run_flag_on_prepends_block(monkeypatch, tmp_path):
    monkeypatch.setattr(gx10, "RAG_ENABLED", True)
    monkeypatch.setattr(gx10, "_MEMORY", _FakeMem(["fact A", "fact B"]))
    monkeypatch.setattr(gx10, "_WARM", None)
    g = _mk_agent(monkeypatch, tmp_path)
    g._generate = lambda think: ("ok", [], False, None, None)
    g.run("what is the fact?")
    last_user = [m for m in g.messages if m.get("role") == "user"][-1]
    assert last_user["content"].startswith(gx10._RAG_MARKER)
    assert "fact A" in last_user["content"]
    assert last_user["content"].endswith("what is the fact?")   # original query preserved at the tail
