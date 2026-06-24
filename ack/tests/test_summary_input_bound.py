"""Epic #366 / P1 (3/3) — bound the summarizer input (#373).

`_summarize` capped only its OUTPUT (`SUMMARY_MAX_TOKENS`), never its INPUT: a large evicted
transcript was fed whole, so the summarizer call itself could hit the model window and vLLM would
silently truncate it → a lossy rolling summary (state loss over long sessions). Now the input is
bounded token-based, tail-first (`input_budget = min(4096, max_model_len // 4)`); the FULL raw is
still archived losslessly to cold FIRST (in `_roll_summary`), then the bounded TAIL is summarized.
Validated WITHOUT a live model:

  * `_bound_text_tail` keeps the most-recent tail within budget (snaps to a round boundary); no-op
    when under budget / zero budget (negative paths);
  * `_summarize` bounds a large transcript before the model call (the most recent rounds survive,
    the oldest are dropped) yet still returns a non-empty summary; a small transcript is untouched;
  * `_roll_summary` archives the FULL raw to cold FIRST, then summarizes only the bounded tail (the
    lossless guarantee — nothing is lost even though the summary input is trimmed).
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
import math  # noqa: E402


class _FakeCounter:
    def __init__(self, cpt=2.0):
        self.cpt = float(cpt)

    def usable(self):
        return True

    def count_text(self, text):
        return int(math.ceil(len(text or "") / self.cpt))

    def count_prompt(self, messages, per_msg_overhead=4):
        return sum(self.count_text(gx10._message_text(m)) + per_msg_overhead for m in messages)


class _CapCompletions:
    def __init__(self):
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=types.SimpleNamespace(content="SUMMARY"))])


class _CapClient:
    def __init__(self):
        self.chat = types.SimpleNamespace(completions=_CapCompletions())


class _FakeMem:
    def __init__(self):
        self.bulk = []

    def is_available(self):
        return True

    def add_bulk(self, text, metadata=None):
        self.bulk.append((text, metadata))


def _mk_agent(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(gx10, "OpenAI", lambda **kw: object())
    g = gx10.GX10(base_url="http://x/v1", api_key="k", model="m", prompt_path="")
    g.client = _CapClient()
    return g


def _big_transcript(n=80, size=200):
    return "\n\n".join(f"[user] TAIL-{i} " + "y" * size for i in range(n))


# ── _bound_text_tail ─────────────────────────────────────────────────────────
def test_bound_text_tail_keeps_recent_within_budget(monkeypatch):
    monkeypatch.setattr(gx10, "_TOKENS", _FakeCounter(2.0))
    text = "\n\n".join(f"[user] round {i} " + "x" * 100 for i in range(50))
    out, trunc = gx10._bound_text_tail(text, 200)
    assert trunc is True
    assert gx10._count_tokens(out) <= 200                  # within budget
    assert "round 49" in out and "round 0 " not in out     # kept the TAIL (most recent), dropped oldest


def test_bound_text_tail_large_leading_round_keeps_budget(monkeypatch):
    # #373 review S3: a large round leading the budgeted slice must NOT collapse the tail to a tiny
    # fragment via the paragraph snap (the first "\n\n" is the boundary to the NEXT round). Keep the
    # budget rather than throwing it away.
    monkeypatch.setattr(gx10, "_TOKENS", _FakeCounter(2.0))
    text = "[user] huge " + "g" * 20000 + "\n\n[assistant] tiny reply"
    out, trunc = gx10._bound_text_tail(text, 2000)
    assert trunc is True
    n = gx10._count_tokens(out)
    assert 2000 * 0.7 <= n <= 2000                  # most of the budget used, not a ~9-token fragment


def test_bound_text_tail_noop_when_under_budget(monkeypatch):
    monkeypatch.setattr(gx10, "_TOKENS", _FakeCounter(2.0))
    assert gx10._bound_text_tail("small text", 1000) == ("small text", False)


def test_bound_text_tail_zero_budget_is_noop(monkeypatch):
    monkeypatch.setattr(gx10, "_TOKENS", _FakeCounter(2.0))
    assert gx10._bound_text_tail("abc", 0) == ("abc", False)        # never errors / empties


def test_bound_text_tail_fallback_estimate(monkeypatch):
    monkeypatch.setattr(gx10, "_TOKENS", None)                       # calibrated estimate path
    text = "\n\n".join(f"[user] r{i} " + "z" * 80 for i in range(40))
    out, trunc = gx10._bound_text_tail(text, 150)
    assert trunc is True and gx10._count_tokens(out) <= 150


# ── _summarize input bound ───────────────────────────────────────────────────
def test_summarize_bounds_large_input_tail_first(monkeypatch, tmp_path):
    monkeypatch.setattr(gx10, "_TOKENS", _FakeCounter(2.0))
    monkeypatch.setattr(gx10, "MAX_MODEL_LEN", 8192)                 # input_budget = min(4096, 2048) = 2048
    g = _mk_agent(monkeypatch, tmp_path)
    huge = _big_transcript(80, 200)

    summary = g._summarize("", huge)

    assert summary == "SUMMARY"                                      # still a valid, non-empty summary
    user = g.client.chat.completions.calls[0]["messages"][1]["content"]
    assert gx10._count_tokens(user) <= 2048 + 40                     # the input was bounded (~budget + instr slack)
    assert "TAIL-79" in user and "TAIL-0 " not in user              # the most recent rounds survived


def test_summarize_small_input_not_bounded(monkeypatch, tmp_path):
    # negative: under budget ⇒ the full transcript is fed verbatim (no truncation)
    monkeypatch.setattr(gx10, "_TOKENS", _FakeCounter(2.0))
    monkeypatch.setattr(gx10, "MAX_MODEL_LEN", 32768)
    g = _mk_agent(monkeypatch, tmp_path)
    raw = "[user] hello there\n\n[assistant] hi back"
    g._summarize("", raw)
    user = g.client.chat.completions.calls[0]["messages"][1]["content"]
    assert raw in user                                              # whole raw present, nothing dropped


def test_summarize_caps_output_unchanged(monkeypatch, tmp_path):
    # the OUTPUT cap is still applied (regression: bounding the input didn't touch max_tokens)
    monkeypatch.setattr(gx10, "_TOKENS", _FakeCounter(2.0))
    monkeypatch.setattr(gx10, "SUMMARY_MAX_TOKENS", 321)
    g = _mk_agent(monkeypatch, tmp_path)
    g._summarize("", "[user] x")
    assert g.client.chat.completions.calls[0]["max_tokens"] == 321


# ── _roll_summary: archive FULL raw first, summarize bounded tail ─────────────
def test_roll_summary_archives_full_raw_then_bounds_summary(monkeypatch, tmp_path):
    monkeypatch.setattr(gx10, "_TOKENS", _FakeCounter(2.0))
    monkeypatch.setattr(gx10, "MAX_MODEL_LEN", 8192)               # input_budget 2048
    mem = _FakeMem()
    monkeypatch.setattr(gx10, "_MEMORY", mem)
    g = _mk_agent(monkeypatch, tmp_path)
    evicted = [{"role": "user", "content": f"OLDEST-{i} " + "y" * 200} for i in range(80)]
    raw_full = g._render_rounds(evicted)

    g._roll_summary([{"role": "system", "content": "SYS"}], evicted)

    # 1) the FULL raw is archived losslessly to cold (nothing lost), incl. the oldest round
    assert len(mem.bulk) == 1
    assert mem.bulk[0][0] == raw_full and "OLDEST-0 " in mem.bulk[0][0]
    # 2) but the summarizer only saw the bounded tail (the oldest round is NOT in the model input)
    user = g.client.chat.completions.calls[0]["messages"][1]["content"]
    assert gx10._count_tokens(user) <= 2048 + 40
    assert "OLDEST-0 " not in user and "OLDEST-79" in user
