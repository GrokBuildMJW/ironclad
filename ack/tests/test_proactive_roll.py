"""#1051 (L3): the proactive cumulative-ingestion accountant + the shared per-turn summarize rate-limit.

Locks:
  * **Default OFF = byte-identical** — `proactive_roll` off, `max_summaries_per_turn` = 0 (unlimited) in the
    code defaults; `_proactive_roll_if_needed` with the flag off is a pure no-op (no eviction, no model call).
  * **Proactive shed** — with the flag on, once the transcript crosses `ingest_soft_frac` of the window the
    oldest WHOLE rounds are shed EARLY via a query-aware roll-summary (high floor) + archived to cold.
  * **Shared cap** — `max_summaries_per_turn` bounds the TOTAL summarizes in a turn across every trigger
    (steady-state roll, emergency rung, proactive); past the cap `_roll_summary` degrades to a plain
    archived drop (no model call). The per-turn counter is asserted.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402


class _FakeCompletions:
    def __init__(self):
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(
            message=types.SimpleNamespace(content=f"SUMMARY#{len(self.calls)}"))])


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


def _mk_agent(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(gx10, "OpenAI", lambda **kw: object())
    return gx10.GX10(base_url="http://x/v1", api_key="k", model="m", prompt_path="")


def _marks(msgs):
    return [m for m in msgs if str(m.get("content") or "").startswith(gx10._SUMMARY_MARKER)]


def _big_transcript(n=5, chars=400):
    msgs = [{"role": "system", "content": "SYS"}]
    for i in range(n):
        msgs.append({"role": "user", "content": f"u{i} " + "a" * chars})
        msgs.append({"role": "assistant", "content": f"r{i} " + "b" * chars})
    return msgs


def test_proactive_defaults_off():
    d = gx10._code_defaults()["context"]
    assert d["proactive_roll"] is False
    assert d["max_summaries_per_turn"] == 0          # 0 ⇒ unlimited (byte-identical)
    assert d["ingest_soft_frac"] == 0.7


def test_summary_budget_ok_unlimited_and_capped(monkeypatch, tmp_path):
    g = _mk_agent(monkeypatch, tmp_path)
    monkeypatch.setattr(gx10, "MAX_SUMMARIES_PER_TURN", 0)       # unlimited
    g._summaries_this_turn = 99
    assert g._summary_budget_ok() is True
    monkeypatch.setattr(gx10, "MAX_SUMMARIES_PER_TURN", 2)
    g._summaries_this_turn = 1
    assert g._summary_budget_ok() is True                        # 1 < 2 → still allowed
    g._summaries_this_turn = 2
    assert g._summary_budget_ok() is False                       # 2 >= 2 → cap hit


def test_note_summary_counts(monkeypatch, tmp_path):
    monkeypatch.setattr(gx10, "CHARS_PER_TOKEN", 4.0)
    g = _mk_agent(monkeypatch, tmp_path)
    g._summaries_this_turn, g._summary_tokens_this_turn = 0, 0
    g._note_summary("Y" * 40)
    assert g._summaries_this_turn == 1 and g._summary_tokens_this_turn == 10   # 40 chars / 4


def test_proactive_off_is_noop(monkeypatch, tmp_path):
    monkeypatch.setattr(gx10, "PROACTIVE_ROLL", False)
    monkeypatch.setattr(gx10, "_count_prompt_tokens",
                        lambda msgs: sum(len(str(m.get("content") or "")) for m in msgs) // 4)
    mem = _FakeMem()
    monkeypatch.setattr(gx10, "_MEMORY", mem)
    g = _mk_agent(monkeypatch, tmp_path)
    fc = _FakeClient()
    g.client = fc
    g._summaries_this_turn = 0
    g.messages = _big_transcript()
    before = [dict(m) for m in g.messages]

    g._proactive_roll_if_needed()

    assert [dict(m) for m in g.messages] == before      # transcript untouched (byte-identical)
    assert fc.chat.completions.calls == [] and mem.bulk == [] and g._summaries_this_turn == 0


def test_proactive_on_sheds_early_via_summary(monkeypatch, tmp_path):
    monkeypatch.setattr(gx10, "PROACTIVE_ROLL", True)
    monkeypatch.setattr(gx10, "SUMMARIZE_EVICTED", True)
    monkeypatch.setattr(gx10, "MAX_SUMMARIES_PER_TURN", 0)       # unlimited → isolate the proactive path
    monkeypatch.setattr(gx10, "MAX_MODEL_LEN", 1000)
    monkeypatch.setattr(gx10, "INGEST_SOFT_FRAC", 0.5)           # soft = 500 tok = 2000 chars
    monkeypatch.setattr(gx10, "CHARS_PER_TOKEN", 4.0)
    monkeypatch.setattr(gx10, "_count_prompt_tokens",
                        lambda msgs: sum(len(str(m.get("content") or "")) for m in msgs) // 4)
    mem = _FakeMem()
    monkeypatch.setattr(gx10, "_MEMORY", mem)
    g = _mk_agent(monkeypatch, tmp_path)
    g.client = _FakeClient()
    g._summaries_this_turn = 0
    g.messages = _big_transcript(n=5, chars=400)                 # ~4000 chars = 1000 tok, well over soft

    g._proactive_roll_if_needed()

    user_rounds = [m for m in g.messages if m.get("role") == "user"]
    assert len(user_rounds) < 5                                  # oldest rounds were shed early
    marks = _marks(g.messages)
    assert len(marks) == 1 and "SUMMARY#1" in marks[0]["content"]   # query-aware summary block added
    assert g._summaries_this_turn == 1
    assert len(mem.bulk) == 1 and mem.bulk[0][1]["source"] == "context_eviction"   # evicted raw archived
    assert gx10._count_prompt_tokens(g.messages) < 550          # back around/under the soft mark


def test_shared_cap_limits_summaries_across_calls(monkeypatch, tmp_path):
    # the shared per-turn cap bounds the TOTAL summarizes; the 2nd roll degrades to a plain archived drop.
    monkeypatch.setattr(gx10, "SUMMARIZE_EVICTED", True)
    monkeypatch.setattr(gx10, "MAX_SUMMARIES_PER_TURN", 1)
    mem = _FakeMem()
    monkeypatch.setattr(gx10, "_MEMORY", mem)
    g = _mk_agent(monkeypatch, tmp_path)
    fc = _FakeClient()
    g.client = fc
    g._summaries_this_turn = 0
    system = [{"role": "system", "content": "SYS"}]
    ev1 = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
    ev2 = [{"role": "user", "content": "c"}, {"role": "assistant", "content": "d"}]

    g._roll_summary(system, ev1)        # budget ok → summarizes (counter → 1)
    g._roll_summary(system, ev2)        # cap hit → plain archived drop, NO 2nd model call

    assert g._summaries_this_turn == 1
    assert len(fc.chat.completions.calls) == 1     # only ONE summarize in the turn
    assert len(mem.bulk) == 2                       # both evictions still archived (drop stays lossless)
    assert len(_marks(system)) == 1                 # exactly one summary block (from the first roll)
