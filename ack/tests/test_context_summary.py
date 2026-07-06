"""B1 — rolling summarization on context eviction (engine/gx10.py + memory.add_bulk).

Locks the load-bearing B1 invariants WITHOUT a live model or mem-api:

  * ``memory.add_bulk`` posts to ``/add_bulk`` (``infer=false``, vector-only), fire-and-forget,
    and is a no-op when memory is unconfigured.
  * **Flag OFF ⇒ byte-identical trim.** ``_trim_context`` with ``SUMMARIZE_EVICTED=False`` produces
    exactly the same ``messages`` as a reference copy of today's algorithm, makes ZERO model calls,
    and adds NO summary block. (The plan's hard requirement.)
  * **Flag ON** rolls evicted rounds into one summary block kept right under the system prompt,
    archives the raw evicted text to cold (``add_bulk``), and on a second trim UPDATES (not
    duplicates) the block, feeding the previous summary back in (hierarchical).
  * **Fail-soft**: a summarizer error degrades to today's plain drop (no block, no raise); the cold
    archive is still attempted. Memory unconfigured ⇒ summary still rolls, no crash.
"""
from __future__ import annotations

import copy
import json
import sys
import time
import types
from pathlib import Path

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402
import memory  # noqa: E402
import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _neutralize_budget_reserve(monkeypatch):
    # #503 BUDGET-1 made the char-fallback trim reserve tools+thinking in the watermark. These tests pin the
    # rolling-SUMMARY behavior against the plain char budget (high=MAX_CTX_CHARS, low=TRIM_TARGET_CHARS), so
    # neutralize the non-system reserve (tools + thinking); the tiny system prompt is harmless (the to-low
    # trim target is unchanged). The reserve math itself is covered in test_token_budget.py.
    monkeypatch.setattr(gx10, "THINKING_RESERVE", 0, raising=False)
    monkeypatch.setattr(gx10, "_tools_schema_tokens", lambda: 0, raising=False)


# ── memory.add_bulk ──────────────────────────────────────────────────────────
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


def test_add_bulk_posts_vector_only_fire_and_forget(monkeypatch):
    captured: dict = {}

    def _urlopen(req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        if url.endswith("/add_bulk"):
            captured["bulk"] = json.loads(req.data.decode("utf-8"))
            captured["timeout"] = timeout
        return _Resp({"results": []})

    monkeypatch.setattr(memory.urllib.request, "urlopen", _urlopen)
    m = memory.MemoryManager({"base_url": "http://mem:8800", "agent_id": "ironclad"})
    m.add_bulk("evicted transcript text", {"k": "v"})
    for _ in range(40):  # the POST runs on a daemon thread
        if "bulk" in captured:
            break
        time.sleep(0.05)
    bulk = captured.get("bulk")
    assert bulk is not None
    assert bulk["infer"] is False                       # vector-only, no LLM extraction
    assert bulk["agent_id"] == "ironclad"
    assert bulk["messages"][0]["content"] == "evicted transcript text"
    assert bulk["metadata"]["source"] == "context_eviction"
    assert bulk["metadata"]["k"] == "v"
    assert captured["timeout"] == m.read_timeout        # short read timeout (bulk has no LLM)


def test_add_bulk_noop_when_unconfigured():
    m = memory.MemoryManager({})
    m.add_bulk("x")           # must not raise
    m.add_bulk("")            # empty → no-op


# ── _trim_context (B1) ───────────────────────────────────────────────────────
def _mk_agent(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(gx10, "OpenAI", lambda **kw: object())
    g = gx10.GX10(base_url="http://x/v1", api_key="k", model="m", prompt_path="")
    return g


def _rounds(n):
    """n user/assistant rounds; round 0 carries a unique eviction sentinel."""
    msgs = []
    for i in range(n):
        tag = "EVICT-SENTINEL " if i == 0 else ""
        msgs.append({"role": "user", "content": f"{tag}user message {i} " + "x" * 90})
        msgs.append({"role": "assistant", "content": f"assistant reply {i} " + "y" * 90})
    return msgs


def _ref_trim(messages, hi, lo):
    """A standalone copy of today's trim algorithm — the byte-identical oracle."""
    def tot(ms):
        return sum(len(str(m.get("content") or "")) for m in ms)
    system = [m for m in messages if m.get("role") == "system"]
    others = [m for m in messages if m.get("role") != "system"]
    if tot(others) <= hi:
        return system + others
    while tot(others) > lo and len(others) > 1:
        cut = 1
        while cut < len(others) and others[cut].get("role") != "user":
            cut += 1
        if cut >= len(others):
            break
        del others[:cut]
    return system + others


class _FakeCompletions:
    def __init__(self, boom=False):
        self.calls = []
        self.boom = boom

    def create(self, **kw):
        self.calls.append(kw)
        if self.boom:
            raise RuntimeError("model down")
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(
                message=types.SimpleNamespace(content=f"SUMMARY#{len(self.calls)}"))])


class _FakeClient:
    def __init__(self, boom=False):
        self.chat = types.SimpleNamespace(completions=_FakeCompletions(boom))


class _FakeMem:
    def __init__(self):
        self.bulk = []

    def is_available(self):
        return True

    def add_bulk(self, text, metadata=None):
        self.bulk.append((text, metadata))


def _markers(msgs):
    return [m for m in msgs if str(m.get("content") or "").startswith(gx10._SUMMARY_MARKER)]


def test_flag_off_is_byte_identical_and_calls_no_model(monkeypatch, tmp_path):
    monkeypatch.setattr(gx10, "MAX_CTX_CHARS", 1000)
    monkeypatch.setattr(gx10, "TRIM_TARGET_CHARS", 400)
    monkeypatch.setattr(gx10, "SUMMARIZE_EVICTED", False)
    g = _mk_agent(monkeypatch, tmp_path)
    fc = _FakeClient()
    g.client = fc
    g.messages = [{"role": "system", "content": "SYS"}] + _rounds(8)
    expected = _ref_trim(copy.deepcopy(g.messages), 1000, 400)

    g._trim_context()

    assert g.messages == expected                  # byte-identical to today's algorithm
    assert _markers(g.messages) == []              # no summary block
    assert fc.chat.completions.calls == []         # zero model calls
    assert len(g.messages) < len([{"role": "system"}] + _rounds(8))  # it actually trimmed


def test_flag_on_rolls_summary_and_archives(monkeypatch, tmp_path):
    monkeypatch.setattr(gx10, "MAX_CTX_CHARS", 1000)
    monkeypatch.setattr(gx10, "TRIM_TARGET_CHARS", 400)
    monkeypatch.setattr(gx10, "SUMMARIZE_EVICTED", True)
    fake_mem = _FakeMem()
    monkeypatch.setattr(gx10, "_MEMORY", fake_mem)
    g = _mk_agent(monkeypatch, tmp_path)
    g.client = _FakeClient()
    g.messages = [{"role": "system", "content": "SYS"}] + _rounds(8)
    # which rounds survive is summary-independent → reuse the oracle for the tail
    survivors = [m for m in _ref_trim(copy.deepcopy(g.messages), 1000, 400)
                 if m.get("role") != "system"]

    g._trim_context()

    marks = _markers(g.messages)
    assert len(marks) == 1                                   # exactly one summary block
    assert "SUMMARY#1" in marks[0]["content"]
    assert marks[0]["role"] == "system"
    assert g.messages[0]["content"] == "SYS"                 # original prompt stays first
    assert g.messages[1] is marks[0]                         # summary sits right under it
    # the verbatim tail is unchanged by summarization
    assert [m for m in g.messages if m.get("role") != "system"] == survivors
    # raw evicted text was archived to cold (vector-only), incl. the sentinel
    assert len(fake_mem.bulk) == 1
    assert "EVICT-SENTINEL" in fake_mem.bulk[0][0]


def test_second_trim_updates_not_duplicates_and_is_hierarchical(monkeypatch, tmp_path):
    monkeypatch.setattr(gx10, "MAX_CTX_CHARS", 1000)
    monkeypatch.setattr(gx10, "TRIM_TARGET_CHARS", 400)
    monkeypatch.setattr(gx10, "SUMMARIZE_EVICTED", True)
    monkeypatch.setattr(gx10, "_MEMORY", _FakeMem())
    g = _mk_agent(monkeypatch, tmp_path)
    fc = _FakeClient()
    g.client = fc
    g.messages = [{"role": "system", "content": "SYS"}] + _rounds(8)
    g._trim_context()                       # first roll → SUMMARY#1
    # grow again past the high-water and trim once more
    g.messages = g.messages + _rounds(8)
    g._trim_context()                       # second roll → SUMMARY#2 (updates the same block)

    marks = _markers(g.messages)
    assert len(marks) == 1                           # updated in place, not duplicated
    assert "SUMMARY#2" in marks[0]["content"]
    # hierarchical: the 2nd summarize call was fed the previous summary
    second_user = fc.chat.completions.calls[1]["messages"][1]["content"]
    assert "PREVIOUS SUMMARY" in second_user and "SUMMARY#1" in second_user


def test_summarizer_failure_is_fail_soft(monkeypatch, tmp_path):
    monkeypatch.setattr(gx10, "MAX_CTX_CHARS", 1000)
    monkeypatch.setattr(gx10, "TRIM_TARGET_CHARS", 400)
    monkeypatch.setattr(gx10, "SUMMARIZE_EVICTED", True)
    fake_mem = _FakeMem()
    monkeypatch.setattr(gx10, "_MEMORY", fake_mem)
    g = _mk_agent(monkeypatch, tmp_path)
    g.client = _FakeClient(boom=True)       # summarize raises
    g.messages = [{"role": "system", "content": "SYS"}] + _rounds(8)
    expected = _ref_trim(copy.deepcopy(g.messages), 1000, 400)

    g._trim_context()                        # must not raise

    assert _markers(g.messages) == []        # no block on failure
    assert g.messages == expected            # degraded to today's plain drop
    assert len(fake_mem.bulk) == 1           # cold archive still attempted (independent)


def test_engine_memory_defaults_are_on():
    # 06-18 decision: B1/B2 + §3c worker memory are active by DEFAULT (no flag needed).
    d = gx10._code_defaults()
    assert d["context"]["summarize_evicted"] is True    # B1
    assert d["context"]["rag_enabled"] is True           # B2
    assert d["workers"]["memory_read"] is True           # §3c MAP
    assert d["workers"]["memory_write"] is True          # §3c REDUCE


def test_b4_window_thresholds_env_override(monkeypatch):
    # B4: env retunes the trim working-set (the new setif lines in _apply_env).
    monkeypatch.setenv("GX10_MAX_CTX_CHARS", "120000")
    monkeypatch.setenv("GX10_TRIM_TARGET_CHARS", "72000")
    cfg = gx10._apply_env(gx10._code_defaults())
    assert cfg["context"]["max_ctx_chars"] == 120000
    assert cfg["context"]["trim_target_chars"] == 72000


def test_b4_window_thresholds_unset_are_defaults(monkeypatch):
    # unset env → byte-identical to today's code defaults (no override)
    monkeypatch.delenv("GX10_MAX_CTX_CHARS", raising=False)
    monkeypatch.delenv("GX10_TRIM_TARGET_CHARS", raising=False)
    defaults = gx10._code_defaults()
    cfg = gx10._apply_env(gx10._code_defaults())
    assert cfg["context"]["max_ctx_chars"] == defaults["context"]["max_ctx_chars"]
    assert cfg["context"]["trim_target_chars"] == defaults["context"]["trim_target_chars"]


def test_flag_on_without_memory_still_rolls(monkeypatch, tmp_path):
    monkeypatch.setattr(gx10, "MAX_CTX_CHARS", 1000)
    monkeypatch.setattr(gx10, "TRIM_TARGET_CHARS", 400)
    monkeypatch.setattr(gx10, "SUMMARIZE_EVICTED", True)
    monkeypatch.setattr(gx10, "_MEMORY", None)   # memory unconfigured
    g = _mk_agent(monkeypatch, tmp_path)
    g.client = _FakeClient()
    g.messages = [{"role": "system", "content": "SYS"}] + _rounds(8)

    g._trim_context()                            # must not crash on _MEMORY is None

    assert len(_markers(g.messages)) == 1
    assert "SUMMARY#1" in _markers(g.messages)[0]["content"]


class _FakeWarm:
    def __init__(self):
        self.sets = []
        self.dels = []

    def set_session(self, sid, field, value, ttl=None):
        self.sets.append((sid, field, value))
        return True

    def del_session(self, sid, field):
        self.dels.append((sid, field))
        return True


def test_roll_summary_mirrors_to_warm(monkeypatch, tmp_path):
    # §3c: the rolling summary is also pushed to the warm tier (durable + shared with workers).
    monkeypatch.setattr(gx10, "MAX_CTX_CHARS", 1000)
    monkeypatch.setattr(gx10, "TRIM_TARGET_CHARS", 400)
    monkeypatch.setattr(gx10, "SUMMARIZE_EVICTED", True)
    monkeypatch.setattr(gx10, "_MEMORY", None)
    warm = _FakeWarm()
    monkeypatch.setattr(gx10, "_WARM", warm)
    monkeypatch.setattr(gx10, "WARM_SESSION_ID", "main")
    g = _mk_agent(monkeypatch, tmp_path)
    g.client = _FakeClient()
    g.messages = [{"role": "system", "content": "SYS"}] + _rounds(8)

    g._trim_context()

    assert any(field == "summary" and "SUMMARY#1" in val for (_, field, val) in warm.sets)


def test_clear_context_drops_warm_summary(monkeypatch, tmp_path):
    # MEM-12: /clear and /reset must also drop the warm rolling summary (else it resurrects).
    warm = _FakeWarm()
    monkeypatch.setattr(gx10, "_WARM", warm)
    monkeypatch.setattr(gx10, "WARM_SESSION_ID", "main")
    g = _mk_agent(monkeypatch, tmp_path)
    g.messages = [{"role": "system", "content": "SYS"},
                  {"role": "system", "content": gx10._SUMMARY_MARKER + "\nold summary"},
                  {"role": "user", "content": "x"}]
    g.clear_context()
    assert g.messages == [{"role": "system", "content": "SYS"}]   # window reset, summary block gone
    assert ("main", "summary") in warm.dels                        # warm summary dropped


def test_clear_context_no_warm_is_fine(monkeypatch, tmp_path):
    monkeypatch.setattr(gx10, "_WARM", None)
    g = _mk_agent(monkeypatch, tmp_path)
    g.messages = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "x"}]
    g.clear_context()                                              # no warm → no crash
    assert g.messages == [{"role": "system", "content": "SYS"}]


def test_rag_toggle_via_dispatch(monkeypatch, tmp_path):
    # MEM-13: `rag off`/`rag on` flips RAG_ENABLED through the server dispatcher.
    g = _mk_agent(monkeypatch, tmp_path)
    monkeypatch.setattr(gx10, "RAG_ENABLED", True)
    gx10._dispatch(g, "rag off")
    assert gx10.RAG_ENABLED is False
    gx10._dispatch(g, "rag on")
    assert gx10.RAG_ENABLED is True


def test_rag_prefix_does_not_hijack_a_real_turn(monkeypatch, tmp_path):
    # a real turn starting with "rag" (e.g. "ragout…") must NOT be caught by the rag toggle
    g = _mk_agent(monkeypatch, tmp_path)
    ran = {}
    g.run = lambda ui: ran.setdefault("ui", ui)  # type: ignore[method-assign]
    monkeypatch.setattr(gx10, "RAG_ENABLED", True)
    gx10._dispatch(g, "ragout recipe please")
    assert ran.get("ui") == "ragout recipe please" and gx10.RAG_ENABLED is True


def test_context_report_shows_summary_and_retrieved(monkeypatch, tmp_path):
    g = _mk_agent(monkeypatch, tmp_path)
    g.messages = [
        {"role": "system", "content": "SYS"},
        {"role": "system", "content": gx10._SUMMARY_MARKER + "\nrolled state here"},
        {"role": "user", "content": gx10._RAG_MARKER + "\n- fact A\n\nwhat is A?"},
    ]
    rep = g.context_report()
    assert "rolling summary" in rep and "rolled state here" in rep
    assert "last retrieved block" in rep and "fact A" in rep


def test_context_report_handles_empty(monkeypatch, tmp_path):
    g = _mk_agent(monkeypatch, tmp_path)
    g.messages = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "hi"}]
    assert "(none)" in g.context_report()   # no summary + no retrieved block


def test_roll_summary_no_warm_is_fine(monkeypatch, tmp_path):
    # no warm tier configured → no crash, summary still rolls in-window
    monkeypatch.setattr(gx10, "MAX_CTX_CHARS", 1000)
    monkeypatch.setattr(gx10, "TRIM_TARGET_CHARS", 400)
    monkeypatch.setattr(gx10, "SUMMARIZE_EVICTED", True)
    monkeypatch.setattr(gx10, "_MEMORY", None)
    monkeypatch.setattr(gx10, "_WARM", None)
    g = _mk_agent(monkeypatch, tmp_path)
    g.client = _FakeClient()
    g.messages = [{"role": "system", "content": "SYS"}] + _rounds(8)
    g._trim_context()
    assert len(_markers(g.messages)) == 1


# ── #1049 (L3): query-aware summary fidelity ─────────────────────────────────
def test_summary_biases_toward_current_turn(monkeypatch, tmp_path):
    # With a user turn in scope, the summarizer instruction gains a query-aware BIAS clause naming the
    # current task (bias, not filter — recency eviction stays unchanged; this only steers the summary).
    monkeypatch.setattr(gx10, "MAX_CTX_CHARS", 1000)
    monkeypatch.setattr(gx10, "TRIM_TARGET_CHARS", 400)
    monkeypatch.setattr(gx10, "SUMMARIZE_EVICTED", True)
    monkeypatch.setattr(gx10, "_MEMORY", _FakeMem())
    g = _mk_agent(monkeypatch, tmp_path)
    fc = _FakeClient()
    g.client = fc
    g._current_user_turn = "deploy the SPARKPLUG-XYZ service to the spark"
    g.messages = [{"role": "system", "content": "SYS"}] + _rounds(8)

    g._trim_context()

    instr = fc.chat.completions.calls[0]["messages"][0]["content"]   # the summarizer's system instruction
    assert "CURRENT task" in instr
    assert "SPARKPLUG-XYZ" in instr


def test_summary_generic_when_no_turn_is_byte_identical(monkeypatch, tmp_path):
    # No turn in scope (fresh agent → "" default): the instruction is the generic one, no query-aware
    # clause — byte-identical to pre-#1049 behaviour.
    monkeypatch.setattr(gx10, "MAX_CTX_CHARS", 1000)
    monkeypatch.setattr(gx10, "TRIM_TARGET_CHARS", 400)
    monkeypatch.setattr(gx10, "SUMMARIZE_EVICTED", True)
    monkeypatch.setattr(gx10, "_MEMORY", _FakeMem())
    g = _mk_agent(monkeypatch, tmp_path)
    fc = _FakeClient()
    g.client = fc
    assert g._current_user_turn == ""          # __init__ default
    g.messages = [{"role": "system", "content": "SYS"}] + _rounds(8)

    g._trim_context()

    instr = fc.chat.completions.calls[0]["messages"][0]["content"]
    assert "CURRENT task" not in instr


def test_run_stores_current_user_turn_at_entry(monkeypatch, tmp_path):
    # run() captures the turn at entry, BEFORE any summarize can fire. Stop right after the assignment
    # (make the first downstream call raise) and assert the field was updated from its stale value.
    g = _mk_agent(monkeypatch, tmp_path)
    g._current_user_turn = "STALE"

    def _boom(_q):
        raise RuntimeError("stop after the entry assignment")

    monkeypatch.setattr(g, "_retrieve_context", _boom)
    with pytest.raises(RuntimeError):
        g.run("fresh topic ABC-123")
    assert g._current_user_turn == "fresh topic ABC-123"
