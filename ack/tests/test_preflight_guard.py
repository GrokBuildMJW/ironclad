"""Epic #366 / P1 (2/3) — pre-flight overflow guard + emergency single-turn trim (#372).

Turns the cryptic raw vLLM 400 (`maximum context length is 32768`) into a guarded, recoverable
path. Before the call, `_make_completion` checks that the prompt + the reserves it must leave free
(output + the tools schema + the CONDITIONAL thinking budget) fit the model window. The output reserve
is a CEILING, not a fixed floor (#366/#379): when the full reserve won't fit, it reserves LESS output —
down to `MIN_OUTPUT_TOKENS` — so a marginal-overflow turn proceeds LOSSLESSLY (all context kept, a
shorter answer) instead of dying. Only when even a minimal answer won't fit does it emergency-trim the
oldest WHOLE rounds, then TRUNCATE an irreducible oversized turn (#994-S16), then — last — raise. Returns
the effective `max_tokens` the request carries. Validated WITHOUT a live model:

  * no-op when it fits; emergency trim of the oldest whole rounds when over (the last user turn and
    the system partition are never dropped; tool rounds stay atomic — no orphan `tool` message);
  * TRUNCATES an irreducible single oversized turn to fit (#994-S16), so an autonomous turn degrades
    gracefully instead of dying (never a raw vLLM 400);
  * the thinking reserve is applied ONLY when `think=True`; the tools schema is reserved;
  * skipped when token budgeting is off (negative path);
  * `_make_completion` runs the guard BEFORE the API call (raises without ever calling vLLM);
  * the evicted rounds are archived losslessly to cold (best-effort).
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
import pytest  # noqa: E402


# ── fakes ────────────────────────────────────────────────────────────────────
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
            choices=[types.SimpleNamespace(message=types.SimpleNamespace(content="ok", tool_calls=None))],
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


def _mk_agent(monkeypatch, tmp_path, max_tokens=512):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(gx10, "OpenAI", lambda **kw: object())
    g = gx10.GX10(base_url="http://x/v1", api_key="k", model="m", prompt_path="")
    g.max_tokens = max_tokens
    return g


def _setup(monkeypatch, *, model_len=4096, tools=0, cpt=2.0, safety=0):
    monkeypatch.setattr(gx10, "TOKEN_BUDGET", True)
    monkeypatch.setattr(gx10, "MAX_MODEL_LEN", model_len)
    monkeypatch.setattr(gx10, "_TOKENS", _FakeCounter(cpt))
    monkeypatch.setattr(gx10, "_tools_schema_tokens", lambda: tools)
    monkeypatch.setattr(gx10, "_MEMORY", None)
    # default the estimate-slop headroom to 0 so a test's budget math is exact; the dedicated
    # safety-margin test sets it explicitly.
    monkeypatch.setattr(gx10, "OVERFLOW_SAFETY_TOKENS", safety)


def _rounds(n, size, fill="x", tool=False):
    out = []
    for i in range(n):
        out.append({"role": "user", "content": f"u{i} " + fill * size})
        if tool:
            out.append({"role": "assistant", "content": None,
                        "tool_calls": [{"id": f"c{i}", "function": {"name": "do", "arguments": "{}"}}]})
            out.append({"role": "tool", "tool_call_id": f"c{i}", "content": "r" * size})
        else:
            out.append({"role": "assistant", "content": f"a{i} " + fill * size})
    return out


# ── pre-flight guard ─────────────────────────────────────────────────────────
def test_preflight_noop_when_it_fits(monkeypatch, tmp_path):
    _setup(monkeypatch)
    g = _mk_agent(monkeypatch, tmp_path)
    msgs = [{"role": "system", "content": "SYS"}] + _rounds(1, 20)
    g.messages = list(msgs)
    g._preflight_context(think=False)
    assert g.messages == msgs                               # fits ⇒ untouched (prefix cache stays)


def test_preflight_emergency_trims_oldest_rounds(monkeypatch, tmp_path):
    _setup(monkeypatch)
    g = _mk_agent(monkeypatch, tmp_path)
    g.messages = [{"role": "system", "content": "SYS"},
                  {"role": "user", "content": "OLDEST-SENTINEL " + "x" * 400}] + _rounds(30, 200)
    g._preflight_context(think=False)
    budget = gx10.MAX_MODEL_LEN - g.max_tokens
    assert gx10._count_prompt_tokens(g.messages) <= budget          # fits the wall now
    assert g.messages[0]["content"] == "SYS"                        # system preserved
    assert any(m.get("role") == "user" for m in g.messages)         # a user turn survives
    assert "OLDEST-SENTINEL" not in str(g.messages)                 # the oldest round was evicted


def test_preflight_truncates_irreducible_single_turn(monkeypatch, tmp_path):
    # #994-S16: a single oversized turn that whole-round eviction can't reduce is now TRUNCATED to fit
    # (head+tail + marker) instead of raising — an autonomous turn degrades gracefully, it does not die.
    _setup(monkeypatch)
    g = _mk_agent(monkeypatch, tmp_path)
    g.messages = [{"role": "system", "content": "SYS"},
                  {"role": "user", "content": "x" * 20000}]         # one oversized turn, no round to evict
    g._preflight_context(think=False)                               # must NOT raise now
    assert gx10._count_prompt_tokens(g.messages) <= gx10.MAX_MODEL_LEN - g.max_tokens   # fits the wall
    assert g.messages[0]["content"] == "SYS"                        # system preserved
    assert "truncated to fit the context window" in g.messages[-1]["content"]           # the turn shrank


def test_preflight_reduces_a_single_turn_agentic_loop(monkeypatch, tmp_path):
    # The real operator failure: an agentic loop is ONE user turn followed by many
    # assistant(tool_calls)+tool(result) rounds with NO new user message. The whole-round evictor cuts
    # only at USER boundaries → finds none → evicts nothing; and truncating a SINGLE oversized message
    # is not enough when many big reads accumulate → the turn raised at ~28k over the wall. The guard
    # must reduce the transcript to fit (iterative truncation of the biggest messages).
    _setup(monkeypatch, model_len=32768, tools=2000)
    monkeypatch.setattr(gx10, "MIN_OUTPUT_TOKENS", 1024)
    g = _mk_agent(monkeypatch, tmp_path, max_tokens=8192)
    msgs = [{"role": "system", "content": "S" * 800}, {"role": "user", "content": "create an epic"}]
    for i in range(12):                                   # 12 big file reads in ONE turn (no user boundary)
        msgs.append({"role": "assistant", "content": None,
                     "tool_calls": [{"id": f"c{i}", "function": {"name": "read_file", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": "F" * 6000})
    g.messages = list(msgs)
    budget = gx10.MAX_MODEL_LEN - 2000 - gx10.MIN_OUTPUT_TOKENS
    assert gx10._count_prompt_tokens(g.messages) > budget           # genuinely over the wall
    eff = g._preflight_context(think=False)                         # must NOT raise
    assert eff >= gx10.MIN_OUTPUT_TOKENS
    assert gx10._count_prompt_tokens(g.messages) <= budget          # reduced to fit
    # tool_calls <-> tool-response pairing stays valid (no orphan tool message)
    open_ids = set()
    for m in g.messages:
        if m.get("role") == "assistant":
            open_ids |= {tc["id"] for tc in (m.get("tool_calls") or [])}
        if m.get("role") == "tool":
            assert m.get("tool_call_id") in open_ids


def test_preflight_thinking_reserve_only_when_think(monkeypatch, tmp_path):
    _setup(monkeypatch, model_len=32768)
    monkeypatch.setattr(gx10, "THINKING_RESERVE", 4000)
    g = _mk_agent(monkeypatch, tmp_path)
    base = [{"role": "system", "content": "SYS"}] + _rounds(20, 600)
    est = gx10._count_prompt_tokens(base)
    # a window that fits WITHOUT the thinking reserve but not WITH it
    monkeypatch.setattr(gx10, "MAX_MODEL_LEN", est + g.max_tokens + 100)

    g.messages = list(base)
    g._preflight_context(think=False)
    assert g.messages == base                                       # think=False ⇒ no reserve, no trim

    g.messages = list(base)
    g._preflight_context(think=True)
    assert len(g.messages) < len(base)                              # think=True ⇒ reserve bites ⇒ trims


def test_preflight_reserves_tools_schema(monkeypatch, tmp_path):
    _setup(monkeypatch, model_len=32768)
    g = _mk_agent(monkeypatch, tmp_path)
    base = [{"role": "system", "content": "SYS"}] + _rounds(20, 600)
    est = gx10._count_prompt_tokens(base)
    monkeypatch.setattr(gx10, "MAX_MODEL_LEN", est + g.max_tokens + 100)  # fits with no tools

    monkeypatch.setattr(gx10, "_tools_schema_tokens", lambda: 0)
    g.messages = list(base)
    g._preflight_context(think=False)
    assert g.messages == base                                       # no tools ⇒ fits, no trim

    monkeypatch.setattr(gx10, "_tools_schema_tokens", lambda: 2000)
    g.messages = list(base)
    g._preflight_context(think=False)
    assert len(g.messages) < len(base)                             # tools reserve bites ⇒ trims


def test_preflight_shrinks_output_reserve_instead_of_trimming(monkeypatch, tmp_path):
    # #366/#379: a MARGINAL overflow — the FULL output reserve won't fit, but there is ample room for a
    # smaller answer — must reserve LESS output and keep ALL context (lossless), NOT trim or raise. The
    # operator hit exactly this: prompt (~21917) + the 8192 reserve + tools was ~11 tok over the 32k wall
    # on a routine multi-file turn, and the turn DIED instead of just reserving 11 fewer output tokens.
    _setup(monkeypatch, model_len=32768, tools=2000)
    monkeypatch.setattr(gx10, "MIN_OUTPUT_TOKENS", 1024)
    g = _mk_agent(monkeypatch, tmp_path, max_tokens=8192)
    base = [{"role": "system", "content": "SYS"}] + _rounds(1, 100)
    est = gx10._count_prompt_tokens(base)
    # a window that leaves 3000 tok of output room: MIN_OUTPUT (1024) <= 3000 < the full reserve (8192)
    monkeypatch.setattr(gx10, "MAX_MODEL_LEN", est + 2000 + 3000)
    g.messages = list(base)
    eff = g._preflight_context(think=False)
    assert g.messages == base                                # LOSSLESS — all context kept, nothing trimmed
    assert eff == 3000                                       # reserved exactly the available output room
    assert gx10.MIN_OUTPUT_TOKENS <= eff < g.max_tokens


def test_preflight_keeps_a_safety_margin_below_the_wall(monkeypatch, tmp_path):
    # #366: `est` undercounts vLLM's exact rendered prompt (chat-template framing + tools/tool-call
    # serialization), so the adaptive clamp must NOT target the wall to the token — it keeps
    # OVERFLOW_SAFETY_TOKENS of headroom, else a zero-margin send still 400s at vLLM (operator hit exactly
    # this: version with the clamp but no margin returned a raw "maximum context length" 400).
    _setup(monkeypatch, model_len=32768, tools=2000, safety=1536)
    monkeypatch.setattr(gx10, "MIN_OUTPUT_TOKENS", 1024)
    g = _mk_agent(monkeypatch, tmp_path, max_tokens=8192)
    base = [{"role": "system", "content": "SYS"}] + _rounds(1, 100)
    est = gx10._count_prompt_tokens(base)
    # window leaves 3000 tok before the safety margin; the effective output is 3000 - 1536 = 1464
    monkeypatch.setattr(gx10, "MAX_MODEL_LEN", est + 2000 + 3000)
    g.messages = list(base)
    eff = g._preflight_context(think=False)
    assert g.messages == base                                # still lossless
    assert eff == 3000 - 1536                                # the safety headroom is left below the wall
    # the actual send leaves >= safety headroom under the window even at the engine's estimate
    assert est + 2000 + eff <= gx10.MAX_MODEL_LEN - 1536


def test_preflight_full_reserve_when_it_fits_returns_ceiling(monkeypatch, tmp_path):
    # when the FULL reserve fits, the effective budget is the ceiling unchanged (no silent shrink).
    _setup(monkeypatch, model_len=32768, tools=0)
    g = _mk_agent(monkeypatch, tmp_path, max_tokens=8192)
    base = [{"role": "system", "content": "SYS"}] + _rounds(1, 50)
    g.messages = list(base)
    assert g._preflight_context(think=False) == 8192
    assert g.messages == base


def test_emergency_trim_keeps_whole_rounds_no_orphan_tool(monkeypatch, tmp_path):
    _setup(monkeypatch)
    g = _mk_agent(monkeypatch, tmp_path)
    g.messages = [{"role": "system", "content": "SYS"}] + _rounds(20, 200, tool=True)
    g._preflight_context(think=False)
    non_sys = [m for m in g.messages if m.get("role") != "system"]
    assert non_sys and non_sys[0].get("role") == "user"           # head is a user — no orphan tool/assistant
    # every tool response still has its matching assistant.tool_calls before it (atomic rounds)
    open_ids = set()
    for m in non_sys:
        if m.get("role") == "assistant":
            open_ids |= {tc["id"] for tc in (m.get("tool_calls") or [])}
        if m.get("role") == "tool":
            assert m.get("tool_call_id") in open_ids               # never orphaned


def test_preflight_skipped_when_token_budget_off(monkeypatch, tmp_path):
    _setup(monkeypatch)
    monkeypatch.setattr(gx10, "TOKEN_BUDGET", False)               # OFF ⇒ guard is a no-op
    g = _mk_agent(monkeypatch, tmp_path)
    big = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "x" * 50000}]
    g.messages = list(big)
    g._preflight_context(think=False)                              # must not raise, must not trim
    assert g.messages == big


def test_preflight_skipped_without_live_tokenizer(monkeypatch, tmp_path):
    # no EXACT tokenizer ⇒ the guard is a no-op: the calibrated estimate over-counts, so trusting it
    # could raise a FALSE ContextOverflowError on input that would actually fit. #371's calibrated
    # _trim_context already budgeted conservatively in this mode.
    _setup(monkeypatch)
    monkeypatch.setattr(gx10, "_TOKENS", None)                    # no live counter
    g = _mk_agent(monkeypatch, tmp_path)
    big = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "x" * 50000}]
    g.messages = list(big)
    g._preflight_context(think=False)                             # must not raise, must not trim
    assert g.messages == big


class _DyingCounter:
    """Live at the gate, then dies on the (die_after+1)-th count_text call — models the tokenizer
    endpoint dropping MID-COUNT (the first failure flips it inert; subsequent counts fall back to the
    over-counting char estimate)."""
    def __init__(self, cpt=2.0, die_after=1):
        self.cpt = float(cpt)
        self.die_after = die_after
        self.calls = 0
        self.dead = False

    def usable(self):
        return not self.dead

    def count_text(self, text):
        if self.dead:
            return None
        self.calls += 1
        if self.calls > self.die_after:
            self.dead = True
            return None
        return int(math.ceil(len(text or "") / self.cpt))

    def count_prompt(self, messages, per_msg_overhead=4):
        total = 0
        for m in messages:
            c = self.count_text(gx10._message_text(m))
            if c is None:
                return None
            total += c + per_msg_overhead
        return total


def test_preflight_aborts_on_tokenizer_death_mid_count(monkeypatch, tmp_path):
    # #372 review S3: the live-counter gate is a one-shot probe; if the tokenizer dies WHILE counting,
    # _count_prompt_tokens silently substitutes the over-counting char estimate → `est` is biased up →
    # without a post-count re-check the guard would needlessly trim or FALSE-raise. The guard must
    # abort (defer to #371's calibrated trim) once the exact tokenizer is gone.
    _setup(monkeypatch)
    monkeypatch.setattr(gx10, "_TOKENS", _DyingCounter(die_after=1))   # dies almost immediately
    g = _mk_agent(monkeypatch, tmp_path)
    # a window large enough that the char-contaminated estimate would exceed the budget (would trim)
    big = [{"role": "system", "content": "SYS"}] + _rounds(20, 300)
    g.messages = list(big)
    g._preflight_context(think=False)                                 # must not raise, must not trim
    assert g.messages == big                                          # aborted on the contaminated count


def test_emergency_trim_archives_evicted_to_cold(monkeypatch, tmp_path):
    _setup(monkeypatch)
    mem = _FakeMem()
    monkeypatch.setattr(gx10, "_MEMORY", mem)
    g = _mk_agent(monkeypatch, tmp_path)
    g.messages = [{"role": "system", "content": "SYS"},
                  {"role": "user", "content": "ARCHIVE-ME " + "x" * 400}] + _rounds(30, 200)
    g._preflight_context(think=False)
    assert len(mem.bulk) == 1                                      # evicted text archived (lossless)
    assert mem.bulk[0][1]["source"] == "emergency_trim"
    assert "ARCHIVE-ME" in mem.bulk[0][0]


# ── _make_completion integration ─────────────────────────────────────────────
def test_make_completion_raises_when_even_truncation_cannot_fit(monkeypatch, tmp_path):
    # a TRULY irreducible turn — the SYSTEM partition alone exceeds the window, so neither whole-round
    # eviction nor content-truncation (#994-S16 preserves the system message) can help → the guard still
    # raises BEFORE the API, never a raw vLLM 400.
    _setup(monkeypatch)
    g = _mk_agent(monkeypatch, tmp_path)
    g.client = _FakeClient()
    g.messages = [{"role": "system", "content": "x" * 200000}]     # system alone overflows any window
    with pytest.raises(gx10.ContextOverflowError):
        g._make_completion(think=False, stream=False)
    assert g.client.chat.completions.calls == []                  # raised BEFORE ever calling vLLM


def test_make_completion_sanitises_a_loaded_malformed_tool_call(monkeypatch, tmp_path):
    # #1039 defense-in-depth: a MALFORMED tool_call reloaded from session.json (a truncated write_file the
    # operator hit) must not 400 the FIRST request after a restart — _make_completion sanitises the whole
    # history's tool-call arguments to valid JSON before the call, so vLLM's json.loads() render can't fail.
    import json as _json
    _setup(monkeypatch, model_len=32768)
    g = _mk_agent(monkeypatch, tmp_path)
    g.client = _FakeClient()
    g.messages = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "make an epic"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "1", "type": "function",
            "function": {"name": "write_file", "arguments": '{"path": "C:\\\\Users\\\\x\\\\epic.md"'}}]},  # truncated
        {"role": "tool", "tool_call_id": "1", "content": "ERROR: malformed JSON"},
    ]
    g._make_completion(think=False, stream=False)                # must NOT raise / must sanitise first
    assert len(g.client.chat.completions.calls) == 1
    for m in g.client.chat.completions.calls[0]["messages"]:
        for tc in (m.get("tool_calls") or []):
            _json.loads(tc["function"]["arguments"])             # every stored arg now renders (valid JSON)


def test_make_completion_trims_then_calls_api(monkeypatch, tmp_path):
    _setup(monkeypatch)
    g = _mk_agent(monkeypatch, tmp_path)
    g.client = _FakeClient()
    g.messages = [{"role": "system", "content": "SYS"}] + _rounds(30, 200)
    pre = len(g.messages)
    g._make_completion(think=False, stream=False)
    assert len(g.client.chat.completions.calls) == 1              # trimmed to fit, then called
    sent = g.client.chat.completions.calls[0]["messages"]
    assert len(sent) < pre and gx10._count_prompt_tokens(sent) <= gx10.MAX_MODEL_LEN - gx10.MIN_OUTPUT_TOKENS


def test_make_completion_sends_the_shrunk_max_tokens(monkeypatch, tmp_path):
    # #366/#379: the request carries the ADAPTIVE output budget (the room actually left), not the fixed
    # ceiling — so a marginal-overflow turn reaches vLLM with a smaller max_tokens instead of dying.
    _setup(monkeypatch, model_len=32768, tools=0)
    monkeypatch.setattr(gx10, "MIN_OUTPUT_TOKENS", 1024)
    g = _mk_agent(monkeypatch, tmp_path, max_tokens=8192)
    g.client = _FakeClient()
    base = [{"role": "system", "content": "SYS"}] + _rounds(1, 100)
    est = gx10._count_prompt_tokens(base)
    monkeypatch.setattr(gx10, "MAX_MODEL_LEN", est + 3000)         # 3000 tok of output room (< 8192 ceiling)
    g.messages = list(base)
    g._make_completion(think=False, stream=False)
    assert len(g.client.chat.completions.calls) == 1
    assert g.client.chat.completions.calls[0]["max_tokens"] == 3000   # shrunk to fit, not the full 8192
    assert g.messages == base                                          # context untouched (lossless)
