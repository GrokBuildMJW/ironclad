"""#1050 (L3): the emergency rung archives the truncated slice + an optional, default-off
summarize-not-truncate with a hard timeout.

Locks:
  * **Always archive** — `_trim_oversized_messages` cold-archives the discarded middle slice
    (`add_bulk(source="fragment_trim")`) so the last-resort truncation stops silently dropping data
    (B2 RAG re-injects it query-aware next turn); with `emergency_summarize` OFF it makes ZERO model calls.
  * **Default OFF** — the flag is off in the code defaults (byte-identical raw head+tail truncation).
  * **Optional summarize** — with the flag ON a fast summarizer replaces the raw drop with a bounded
    summary; a summarizer that RAISES or TIMES OUT always falls through to raw truncation, at most one
    model call per invocation, and recovery is bounded by the configured timeout.
  * **Skip on a sick endpoint** — when a generation this turn already errored, the summarize is skipped.
"""
from __future__ import annotations

import sys
import time
import types
from pathlib import Path

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402
import pytest  # noqa: E402


class _FakeCompletions:
    def __init__(self, content="TIDY-SUMMARY", boom=False, sleep=0.0):
        self.calls = []
        self._content, self._boom, self._sleep = content, boom, sleep

    def create(self, **kw):
        self.calls.append(kw)
        if self._sleep:
            time.sleep(self._sleep)
        if self._boom:
            raise RuntimeError("summarizer down")
        return types.SimpleNamespace(choices=[types.SimpleNamespace(
            message=types.SimpleNamespace(content=self._content))])


class _FakeClient:
    def __init__(self, content="TIDY-SUMMARY", boom=False, sleep=0.0):
        self.chat = types.SimpleNamespace(completions=_FakeCompletions(content, boom, sleep))


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


@pytest.fixture
def _deterministic_tokens(monkeypatch):
    # tokens ≈ total chars / 4, so a truncation deterministically reduces the estimate below the budget.
    monkeypatch.setattr(gx10, "CHARS_PER_TOKEN", 4.0)
    monkeypatch.setattr(gx10, "_count_prompt_tokens",
                        lambda msgs: sum(len(str(m.get("content") or "")) for m in msgs) // 4)


def _oversized_messages():
    return [{"role": "system", "content": "SYS"},
            {"role": "user", "content": "u"},
            {"role": "assistant", "content": "X" * 8000}]


def test_emergency_summarize_defaults_off():
    assert gx10._code_defaults()["context"]["emergency_summarize"] is False


def test_fragment_trim_archives_slice_and_makes_no_model_call(monkeypatch, tmp_path, _deterministic_tokens):
    monkeypatch.setattr(gx10, "EMERGENCY_SUMMARIZE", False)
    mem = _FakeMem()
    monkeypatch.setattr(gx10, "_MEMORY", mem)
    g = _mk_agent(monkeypatch, tmp_path)
    fc = _FakeClient()
    g.client = fc
    g.messages = _oversized_messages()

    est = g._trim_oversized_messages(600)

    assert est <= 600                                             # it actually fit
    body = g.messages[2]["content"]
    assert "chars truncated to fit the context window" in body    # raw marker (summarize OFF)
    assert "summarized to fit" not in body
    assert fc.chat.completions.calls == []                        # NO model call when off
    assert len(mem.bulk) == 1 and mem.bulk[0][1]["source"] == "fragment_trim"   # the slice is archived
    assert "XXXXXXXXXX" in mem.bulk[0][0]                          # the archived slice is the discarded run


def test_no_memory_is_fail_soft(monkeypatch, tmp_path, _deterministic_tokens):
    monkeypatch.setattr(gx10, "EMERGENCY_SUMMARIZE", False)
    monkeypatch.setattr(gx10, "_MEMORY", None)                    # memory unconfigured
    g = _mk_agent(monkeypatch, tmp_path)
    g.client = _FakeClient()
    g.messages = _oversized_messages()
    est = g._trim_oversized_messages(600)                         # must not crash on _MEMORY is None
    assert est <= 600 and "chars truncated to fit" in g.messages[2]["content"]


def test_emergency_summarize_success_replaces_with_summary(monkeypatch, tmp_path, _deterministic_tokens):
    monkeypatch.setattr(gx10, "EMERGENCY_SUMMARIZE", True)
    mem = _FakeMem()
    monkeypatch.setattr(gx10, "_MEMORY", mem)
    g = _mk_agent(monkeypatch, tmp_path)
    g.client = _FakeClient(content="TIDY-SUMMARY")
    g._turn_gen_errored = False
    g.messages = _oversized_messages()

    g._trim_oversized_messages(600)

    body = g.messages[2]["content"]
    assert "summarized to fit" in body and "TIDY-SUMMARY" in body    # summary replaced the raw drop
    assert len(mem.bulk) == 1 and mem.bulk[0][1]["source"] == "fragment_trim"   # still archived (always)


def test_emergency_summarize_exception_falls_through_to_raw(monkeypatch, tmp_path, _deterministic_tokens):
    monkeypatch.setattr(gx10, "EMERGENCY_SUMMARIZE", True)
    monkeypatch.setattr(gx10, "_MEMORY", _FakeMem())
    g = _mk_agent(monkeypatch, tmp_path)
    g.client = _FakeClient(boom=True)             # the summarizer raises
    g._turn_gen_errored = False
    g.messages = _oversized_messages()

    est = g._trim_oversized_messages(600)         # must not raise; recovery still terminates

    assert est <= 600
    body = g.messages[2]["content"]
    assert "chars truncated to fit" in body and "summarized to fit" not in body   # raw fall-through


def test_emergency_summarize_timeout_falls_through_and_is_bounded(monkeypatch, tmp_path, _deterministic_tokens):
    monkeypatch.setattr(gx10, "EMERGENCY_SUMMARIZE", True)
    monkeypatch.setattr(gx10, "EMERGENCY_SUMMARIZE_TIMEOUT_S", 0.2)   # hard cap well below the sleep
    monkeypatch.setattr(gx10, "_MEMORY", _FakeMem())
    g = _mk_agent(monkeypatch, tmp_path)
    g.client = _FakeClient(sleep=1.0)             # the summarizer blocks longer than the timeout
    g._turn_gen_errored = False
    g.messages = _oversized_messages()

    t0 = time.monotonic()
    g._trim_oversized_messages(600)
    elapsed = time.monotonic() - t0

    body = g.messages[2]["content"]
    assert "chars truncated to fit" in body and "summarized to fit" not in body   # raw fall-through on timeout
    assert elapsed < 0.8                          # bounded — did NOT wait the full 1.0s summarizer sleep


def test_emergency_summarize_skipped_when_turn_errored(monkeypatch, tmp_path, _deterministic_tokens):
    monkeypatch.setattr(gx10, "EMERGENCY_SUMMARIZE", True)
    monkeypatch.setattr(gx10, "_MEMORY", _FakeMem())
    g = _mk_agent(monkeypatch, tmp_path)
    fc = _FakeClient(content="SHOULD-NOT-APPEAR")
    g.client = fc
    g._turn_gen_errored = True                    # a generation this turn already errored → skip the summarize

    g.messages = _oversized_messages()
    g._trim_oversized_messages(600)

    body = g.messages[2]["content"]
    assert "chars truncated to fit" in body and "SHOULD-NOT-APPEAR" not in body
    assert fc.chat.completions.calls == []        # NO model call on the sick-endpoint skip
