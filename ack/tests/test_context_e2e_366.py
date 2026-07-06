"""#366 file-heavy repro — the epic #1043 C2 keystone (end-to-end, offline).

The design's load-bearing acceptance artifact: **N sequential LARGE reads in ONE user turn stay under the
served 32k wall** — the durable floor holds and the turn never dies with a raw vLLM 400. This drives the
REAL machinery per tool round, in the order the chat loop does it:

  1. **L1** — each read result is capped at the ingestion choke point (`_cap_ingested_result`) to the LIVE
     per-turn budget (`_live_read_budget`), which SHRINKS as the window fills, so no single read overflows;
  2. append the round (assistant `tool_calls` + the capped `tool` result);
  3. **L3 proactive** — `_proactive_roll_if_needed`; **L3 steady-state** — `_trim_context`;
  4. **the pre-flight guard** — `_preflight_context`, which runs before EVERY real send and is the invariant
     that guarantees the fit.

**Honest architecture note (surfaced by this fixture):** a file-heavy turn is ONE user message followed by
many `assistant(tool_calls)` + `tool` rounds with NO intervening user boundary. The L3 whole-round shed
(`_trim_context`) and the proactive accountant both cut at USER boundaries, so on a single-user-turn loop
they cannot evict. What holds the wall here is therefore **L1 (the shrinking per-read cap) + the L3 EMERGENCY
rung** inside `_preflight_context` (`_emergency_trim` → `_trim_oversized_messages`, which fragment-truncates
the biggest accumulated reads AND — since #1050 — archives the discarded slices losslessly to cold). The
graceful whole-round shed / roll-summary is exercised by the multi-turn tests (`test_context_summary.py`).
The live latency budget is measured by the operator harness `deploy/spark/ctx_harness.py`.
"""
from __future__ import annotations

import math
import sys
import types
from pathlib import Path

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=object))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402


class _FakeCounter:
    def __init__(self, cpt=2.0):
        self.cpt = float(cpt)

    def usable(self):
        return True

    def count_text(self, text):
        return int(math.ceil(len(text or "") / self.cpt))

    def count_prompt(self, messages, per_msg_overhead=4):
        return sum(self.count_text(gx10._message_text(m)) + per_msg_overhead for m in messages)


class _FakeCompletions:
    def __init__(self):
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=types.SimpleNamespace(content="rolling summary", tool_calls=None))],
            usage=None)


class _FakeClient:
    def __init__(self):
        self.chat = types.SimpleNamespace(completions=_FakeCompletions())


class _FakeMem:
    def __init__(self):
        self.bulk = []

    def is_available(self):
        return True

    def add_bulk(self, text, metadata=None):
        self.bulk.append((text, metadata))


def _mk_agent(monkeypatch, tmp_path, max_tokens=8192):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(gx10, "OpenAI", lambda **kw: object())
    g = gx10.GX10(base_url="http://x/v1", api_key="k", model="m", prompt_path="")
    g.max_tokens = max_tokens
    return g


def _setup(monkeypatch, *, model_len=32768, tools=2000, cpt=2.0):
    monkeypatch.setattr(gx10, "TOKEN_BUDGET", True)
    monkeypatch.setattr(gx10, "MAX_MODEL_LEN", model_len)
    monkeypatch.setattr(gx10, "_TOKENS", _FakeCounter(cpt))
    monkeypatch.setattr(gx10, "_tools_schema_tokens", lambda: tools)
    monkeypatch.setattr(gx10, "MIN_OUTPUT_TOKENS", 1024)
    monkeypatch.setattr(gx10, "OVERFLOW_SAFETY_TOKENS", 0)   # exact budget math (the safety margin has its own test)


def _drive_file_heavy_turn(g, *, reads, tools):
    """Simulate ONE user turn of `reads` huge file reads through the real per-round path. After each round the
    pre-flight guard (which runs before every real send) MUST make the turn fit and NEVER raise — the
    'stays under the 32k wall' invariant. Returns the peak prompt tokens observed post-guard."""
    peak = 0
    for i in range(reads):
        cap = g._live_read_budget()                                     # L1: the live, shrinking per-read budget
        capped = gx10._cap_ingested_result("read_file", "F" * 200_000, cap)
        assert len(capped) <= cap + gx10._INGEST_MARKER_SLACK, f"round {i}: L1 cap exceeded"
        g.messages.append({"role": "assistant", "content": None,
                           "tool_calls": [{"id": f"c{i}", "function": {"name": "read_file", "arguments": "{}"}}]})
        g.messages.append({"role": "tool", "tool_call_id": f"c{i}", "content": capped})
        g._proactive_roll_if_needed()                                   # L3 proactive
        g._trim_context()                                              # L3 steady-state
        eff = g._preflight_context(think=False)                        # THE guard — never raises ContextOverflowError
        assert eff >= gx10.MIN_OUTPUT_TOKENS, f"round {i}: no room for even a minimal answer"
        prompt_tok = gx10._count_prompt_tokens(g.messages)
        assert prompt_tok + tools + gx10.MIN_OUTPUT_TOKENS <= gx10.MAX_MODEL_LEN, f"round {i}: over the 32k wall"
        peak = max(peak, prompt_tok)
    return peak


def test_366_file_heavy_turn_stays_under_the_32k_wall(monkeypatch, tmp_path):
    _setup(monkeypatch)
    monkeypatch.setattr(gx10, "_MEMORY", _FakeMem())
    g = _mk_agent(monkeypatch, tmp_path)
    g.client = _FakeClient()
    g.messages = [{"role": "system", "content": "SYS"},
                  {"role": "user", "content": "audit this whole module and explain the auth flow"}]
    g._current_user_turn = g.messages[-1]["content"]

    peak = _drive_file_heavy_turn(g, reads=30, tools=2000)             # 30 huge reads in ONE turn, never raises

    assert peak > 0
    assert g.messages[0]["content"] == "SYS"                          # the system prompt is never dropped
    assert any(m.get("role") == "user" for m in g.messages)          # the user turn survives


def test_366_shed_slices_archived_losslessly_to_cold(monkeypatch, tmp_path):
    # #1050: the reads shed to fit the wall are archived to cold (source in {emergency_trim, fragment_trim}),
    # so nothing is silently lost — B2 RAG can re-inject them query-aware next turn.
    _setup(monkeypatch)
    mem = _FakeMem()
    monkeypatch.setattr(gx10, "_MEMORY", mem)
    g = _mk_agent(monkeypatch, tmp_path)
    g.client = _FakeClient()
    g.messages = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "read the whole repo"}]

    _drive_file_heavy_turn(g, reads=30, tools=2000)

    assert len(mem.bulk) >= 1                                          # discarded content archived, not dropped
    assert all(md.get("source") in ("emergency_trim", "fragment_trim") for _, md in mem.bulk)


def test_366_holds_without_memory_or_summarizer(monkeypatch, tmp_path):
    # the wall invariant must NOT depend on memory or the summarizer being reachable: with no memory (no
    # archive) and no roll-summary (plain drop), L1 + the emergency fragment truncation still hold the turn.
    _setup(monkeypatch)
    monkeypatch.setattr(gx10, "_MEMORY", None)
    monkeypatch.setattr(gx10, "SUMMARIZE_EVICTED", False)
    g = _mk_agent(monkeypatch, tmp_path)
    g.client = _FakeClient()
    g.messages = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "grep the codebase"}]

    _drive_file_heavy_turn(g, reads=25, tools=2000)                   # still never raises, still fits

    assert g.client.chat.completions.calls == []                      # no summarizer call on the plain-drop path
