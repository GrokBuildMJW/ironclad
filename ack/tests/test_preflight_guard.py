"""Epic #366 / P1 (2/3) — pre-flight overflow guard + emergency single-turn trim (#372).

Turns the cryptic raw vLLM 400 (`maximum context length is 32768`) into a guarded, recoverable
path. Before the call, `_make_completion` checks that the prompt + the reserves it must leave free
(output `self.max_tokens` + the tools schema + the CONDITIONAL thinking budget) fit the model window;
if not it does ONE emergency trim of the oldest WHOLE rounds, and if it still can't fit it raises a
clear `ContextOverflowError`. Validated WITHOUT a live model:

  * no-op when it fits; emergency trim of the oldest whole rounds when over (the last user turn and
    the system partition are never dropped; tool rounds stay atomic — no orphan `tool` message);
  * raises `ContextOverflowError` on an irreducible single oversized turn (never a raw vLLM 400);
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


def _setup(monkeypatch, *, model_len=4096, tools=0, cpt=2.0):
    monkeypatch.setattr(gx10, "TOKEN_BUDGET", True)
    monkeypatch.setattr(gx10, "MAX_MODEL_LEN", model_len)
    monkeypatch.setattr(gx10, "_TOKENS", _FakeCounter(cpt))
    monkeypatch.setattr(gx10, "_tools_schema_tokens", lambda: tools)
    monkeypatch.setattr(gx10, "_MEMORY", None)


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


def test_preflight_raises_on_irreducible_single_turn(monkeypatch, tmp_path):
    _setup(monkeypatch)
    g = _mk_agent(monkeypatch, tmp_path)
    g.messages = [{"role": "system", "content": "SYS"},
                  {"role": "user", "content": "x" * 20000}]         # one turn, can't trim below it
    with pytest.raises(gx10.ContextOverflowError) as ei:
        g._preflight_context(think=False)
    assert "context overflow" in str(ei.value) and str(gx10.MAX_MODEL_LEN) in str(ei.value)


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
def test_make_completion_runs_guard_before_api_and_raises(monkeypatch, tmp_path):
    _setup(monkeypatch)
    g = _mk_agent(monkeypatch, tmp_path)
    g.client = _FakeClient()
    g.messages = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "x" * 20000}]
    with pytest.raises(gx10.ContextOverflowError):
        g._make_completion(think=False, stream=False)
    assert g.client.chat.completions.calls == []                  # raised BEFORE ever calling vLLM


def test_make_completion_trims_then_calls_api(monkeypatch, tmp_path):
    _setup(monkeypatch)
    g = _mk_agent(monkeypatch, tmp_path)
    g.client = _FakeClient()
    g.messages = [{"role": "system", "content": "SYS"}] + _rounds(30, 200)
    pre = len(g.messages)
    g._make_completion(think=False, stream=False)
    assert len(g.client.chat.completions.calls) == 1              # trimmed to fit, then called
    sent = g.client.chat.completions.calls[0]["messages"]
    assert len(sent) < pre and gx10._count_prompt_tokens(sent) <= gx10.MAX_MODEL_LEN - g.max_tokens
