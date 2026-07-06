"""Budget-aware read cap + emergency-trim recovery (#994-S16), offline.

An agent must not be able to self-overflow the model window with a single tool result. Pins: `read_file`
honours the live per-turn char cap (a contextvar) instead of the fixed ceiling; `_live_read_budget` shrinks
with the transcript and fails soft without an exact tokenizer; and `_emergency_trim` fits an irreducible
single oversized turn by TRUNCATING it (a marker), instead of leaving the caller to raise.
"""
from __future__ import annotations

import sys
import types

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=object))

from pathlib import Path  # noqa: E402

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402
import pytest  # noqa: E402


def test_read_char_cap_default_and_contextvar():
    assert gx10._read_char_cap() == gx10.MAX_FILE_CHARS          # no live budget → the fixed ceiling
    tok = gx10._READ_BUDGET_CV.set(5_000)
    try:
        assert gx10._read_char_cap() == 5_000                    # the session's live cap wins
    finally:
        gx10._READ_BUDGET_CV.reset(tok)
    assert gx10._read_char_cap() == gx10.MAX_FILE_CHARS          # reset → back to the ceiling


def test_read_file_honours_the_live_cap(tmp_path):
    big = tmp_path / "big.txt"
    big.write_text("x" * 60_000, encoding="utf-8")
    tok = gx10._READ_BUDGET_CV.set(5_000)
    try:
        out = gx10.run_tool("read_file", {"path": str(big)})
    finally:
        gx10._READ_BUDGET_CV.reset(tok)
    assert "capped at 5000" in out and len(out) < 6_000          # bounded to the live cap, not 24k
    # with no live budget the fixed ceiling applies (still capped, but at 24k)
    out2 = gx10.run_tool("read_file", {"path": str(big)})
    assert "capped at 24000" in out2


def _fake(messages, max_tokens=8192):
    return types.SimpleNamespace(messages=messages, max_tokens=max_tokens)


def test_live_read_budget_failsoft_without_tokenizer(monkeypatch):
    # no exact tokenizer (the calibrated estimate over-counts) → don't starve a read that would fit
    monkeypatch.setattr(gx10, "_live_token_counter", lambda: None)
    assert gx10.GX10._live_read_budget(_fake([{"role": "user", "content": "hi"}])) == gx10.MAX_FILE_CHARS
    monkeypatch.setattr(gx10, "_live_token_counter", lambda: object())
    monkeypatch.setattr(gx10, "TOKEN_BUDGET", False)             # budgeting off → fixed ceiling
    assert gx10.GX10._live_read_budget(_fake([{"role": "user", "content": "hi"}])) == gx10.MAX_FILE_CHARS


def test_live_read_budget_shrinks_with_transcript(monkeypatch):
    monkeypatch.setattr(gx10, "TOKEN_BUDGET", True)
    monkeypatch.setattr(gx10, "_live_token_counter", lambda: object())
    monkeypatch.setattr(gx10, "_tools_schema_tokens", lambda: 2_000)
    # a MEDIUM transcript leaves room for a reduced read (below the ceiling, above the floor)
    monkeypatch.setattr(gx10, "_count_prompt_tokens", lambda msgs: 10_000)
    mid = gx10.GX10._live_read_budget(_fake([], max_tokens=8192))
    assert gx10._READ_FLOOR_CHARS < mid < gx10.MAX_FILE_CHARS
    # a NEARLY-FULL transcript → the read collapses to the floor (a small excerpt; emergency-trim backstops)
    monkeypatch.setattr(gx10, "_count_prompt_tokens", lambda msgs: 30_000)
    assert gx10.GX10._live_read_budget(_fake([], max_tokens=8192)) == gx10._READ_FLOOR_CHARS


class _FakeGX10:
    _emergency_trim = gx10.GX10._emergency_trim
    _trim_oversized_messages = gx10.GX10._trim_oversized_messages
    _archive_trimmed_slice = gx10.GX10._archive_trimmed_slice   # #1050: rung-2 archives the discarded slice
    _TRUNCATE_FLOOR_CHARS = gx10.GX10._TRUNCATE_FLOOR_CHARS

    def __init__(self, messages):
        self.messages = messages

    def _render_rounds(self, rounds):     # stub — not reached (no eviction in the single-round case)
        return ""


def test_emergency_trim_truncates_irreducible_oversized_message():
    # one round whose tool result is far too big to fit — whole-round eviction can't help (only 1 round),
    # so the recovery must TRUNCATE the oversized content instead of leaving the caller to raise.
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "read the big file"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "1", "type": "function",
                                                             "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "1", "content": "H" * 100_000},
    ]
    f = _FakeGX10(messages)
    budget = 5_000
    est = f._emergency_trim(budget)
    assert est <= budget                                          # the turn now fits
    tool_content = f.messages[-1]["content"]
    assert "truncated to fit the context window" in tool_content  # the marker
    assert len(tool_content) < 100_000                            # it actually shrank
    assert tool_content.startswith("H") and tool_content.endswith("H")  # head+tail kept


def test_emergency_trim_noop_when_it_already_fits():
    f = _FakeGX10([{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}])
    before = [dict(m) for m in f.messages]
    f._emergency_trim(100_000)                                    # plenty of budget → nothing truncated
    assert [dict(m) for m in f.messages] == before
