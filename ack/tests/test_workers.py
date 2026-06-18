"""Reasoning workers (engine/workers.py) — server-side fan-out.

Validates the fan-out semantics WITHOUT a real model (the OpenAI client is stubbed):
real concurrency (N prompts run at once, wall-clock ≪ sum of latencies), results in
input order, and per-prompt error isolation (one failure does not sink the batch).
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import pytest  # noqa: E402
from workers import ReasoningWorkers  # noqa: E402


class _Msg:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.message = _Msg(content)


class _Usage:
    def __init__(self, ct):
        self.completion_tokens = ct


class _Resp:
    def __init__(self, content, ct=7):
        self.choices = [_Choice(content)]
        self.usage = _Usage(ct)


class _StubClient:
    """Records concurrency; echoes the prompt back as content."""

    def __init__(self, delay=0.0, fail_on=None):
        self.delay = delay
        self.fail_on = fail_on or set()
        self._live = 0
        self.max_live = 0
        self._lock = threading.Lock()
        self.chat = self  # client.chat.completions.create chain
        self.completions = self

    def create(self, *, model, messages, temperature, max_tokens, extra_body):
        prompt = messages[-1]["content"]
        with self._lock:
            self._live += 1
            self.max_live = max(self.max_live, self._live)
        try:
            if self.delay:
                time.sleep(self.delay)
            if prompt in self.fail_on:
                raise RuntimeError(f"boom:{prompt}")
            return _Resp(f"echo:{prompt}")
        finally:
            with self._lock:
                self._live -= 1


def test_results_in_input_order():
    client = _StubClient()
    w = ReasoningWorkers(client, "m", max_concurrency=4)
    res = w.fanout(["a", "b", "c"])
    assert [r["content"] for r in res] == ["echo:a", "echo:b", "echo:c"]
    assert all(r["ok"] for r in res)
    assert res[0]["completion_tokens"] == 7


def test_empty_prompts_returns_empty():
    assert ReasoningWorkers(_StubClient(), "m").fanout([]) == []


def test_runs_concurrently():
    # 6 prompts, each sleeps 0.2s, concurrency 6 → wall-clock ~0.2s, not 1.2s.
    client = _StubClient(delay=0.2)
    w = ReasoningWorkers(client, "m", max_concurrency=6)
    t0 = time.monotonic()
    res = w.fanout([str(i) for i in range(6)])
    elapsed = time.monotonic() - t0
    assert len(res) == 6 and all(r["ok"] for r in res)
    assert client.max_live == 6        # alle gleichzeitig in-flight
    assert elapsed < 0.8               # weit unter 6 * 0.2 = 1.2s


def test_concurrency_is_bounded():
    client = _StubClient(delay=0.1)
    w = ReasoningWorkers(client, "m", max_concurrency=2)
    w.fanout([str(i) for i in range(6)])
    assert client.max_live <= 2        # nie mehr als der Cap gleichzeitig


def test_error_isolation_keeps_order():
    client = _StubClient(fail_on={"b"})
    w = ReasoningWorkers(client, "m", max_concurrency=3)
    res = w.fanout(["a", "b", "c"])
    assert res[0]["ok"] and res[0]["content"] == "echo:a"
    assert not res[1]["ok"] and "boom:b" in res[1]["error"] and res[1]["content"] is None
    assert res[2]["ok"] and res[2]["content"] == "echo:c"


def test_plan_concurrency_envelope():
    # Envelope = concurrency × max_tokens ≤ max_batch_tokens. A large per-call token
    # count lowers the effective parallelism so the GPU is never over-subscribed.
    w = ReasoningWorkers(_StubClient(), "m", max_concurrency=8, max_batch_tokens=8192)
    assert w._plan_concurrency(20, 1024) == 8     # 8192//1024=8 → full concurrency
    assert w._plan_concurrency(20, 2048) == 4     # 8192//2048=4 → halved
    assert w._plan_concurrency(20, 9000) == 1     # one oversized call still runs, alone
    assert w._plan_concurrency(3, 256) == 3       # request size is the binding cap here


def test_envelope_caps_live_concurrency():
    # max_batch_tokens 2000, max_tokens 1000 → budget cap 2, even with concurrency 8.
    client = _StubClient(delay=0.1)
    w = ReasoningWorkers(client, "m", max_concurrency=8, max_batch_tokens=2000)
    w.fanout([str(i) for i in range(6)], max_tokens=1000)
    assert client.max_live <= 2                   # governor held the line, no overload


def test_small_tokens_use_full_concurrency():
    client = _StubClient(delay=0.1)
    w = ReasoningWorkers(client, "m", max_concurrency=8, max_batch_tokens=8192)
    w.fanout([str(i) for i in range(8)], max_tokens=1024)
    assert client.max_live == 8                   # within budget → full batch width


def test_think_flag_forwarded():
    seen = {}

    class _C:
        chat = completions = None

        def create(self, *, model, messages, temperature, max_tokens, extra_body):
            seen.update(extra_body["chat_template_kwargs"])
            return _Resp("x")

    c = _C()
    c.chat = c
    c.completions = c
    ReasoningWorkers(c, "m").fanout(["p"], think=False)
    assert seen == {"enable_thinking": False}
