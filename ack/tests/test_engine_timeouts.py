import httpx
import pytest
import sys
import types
from pathlib import Path

sys.modules.setdefault('openai', types.SimpleNamespace(OpenAI=lambda **kw: object()))
_ENGINE = Path(__file__).resolve().parents[2] / 'engine'
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402


def test_client_timeout_is_bare_float_when_first_token_knob_is_unset(monkeypatch):
    monkeypatch.setattr(gx10, "LLM_REQUEST_TIMEOUT_S", 120.0)
    monkeypatch.setattr(gx10, "LLM_CONNECT_TIMEOUT_S", None)
    monkeypatch.setattr(gx10, "LLM_FIRST_TOKEN_TIMEOUT_S", None)

    timeout = gx10._client_timeout()

    assert timeout == 120.0
    assert isinstance(timeout, float)


def test_client_timeout_decouples_httpx_read_and_connect(monkeypatch):
    monkeypatch.setattr(gx10, "LLM_REQUEST_TIMEOUT_S", 120.0)
    monkeypatch.setattr(gx10, "LLM_CONNECT_TIMEOUT_S", 10.0)
    monkeypatch.setattr(gx10, "LLM_FIRST_TOKEN_TIMEOUT_S", 600.0)

    timeout = gx10._client_timeout()

    assert isinstance(timeout, httpx.Timeout)
    assert timeout.connect == 10.0
    assert timeout.read == 600.0
    assert timeout.write == 120.0
    assert timeout.pool == 120.0


def test_idle_limit_is_phase_aware_only_when_decoupled(monkeypatch):
    monkeypatch.setattr(gx10, "TURN_IDLE_TIMEOUT_S", 240.0)
    monkeypatch.setattr(gx10, "LLM_FIRST_TOKEN_TIMEOUT_S", 600.0)

    assert gx10._idle_limit(first_token_seen=False) == 840.0
    assert gx10._idle_limit(first_token_seen=True) == 240.0

    monkeypatch.setattr(gx10, "LLM_FIRST_TOKEN_TIMEOUT_S", None)
    assert gx10._idle_limit(first_token_seen=False) == 240.0
    assert gx10._idle_limit(first_token_seen=True) == 240.0


def test_timeout_helper_truth_tables(monkeypatch):
    assert gx10._opt_float(None) is None
    assert gx10._opt_float("") is None
    assert gx10._opt_float("auto") is None
    assert gx10._opt_float("none") is None
    assert gx10._opt_float("banana") is None
    assert gx10._opt_float("10") == 10.0
    assert gx10._opt_float("600") == 600.0

    monkeypatch.setattr(gx10, "LLM_FIRST_TOKEN_TIMEOUT_S", None)
    assert gx10._decoupled() is False
    monkeypatch.setattr(gx10, "LLM_FIRST_TOKEN_TIMEOUT_S", 0.0)
    assert gx10._decoupled() is False
    monkeypatch.setattr(gx10, "LLM_FIRST_TOKEN_TIMEOUT_S", 600.0)
    assert gx10._decoupled() is True

    assert gx10._is_timeout_error(httpx.ReadTimeout("read timed out")) is True
    assert gx10._is_timeout_error(ValueError("not a timeout")) is False


class _FakeCompletions:
    def __init__(self, calls, outcomes):
        self._calls = calls
        self._outcomes = outcomes

    def create(self, **kwargs):
        self._calls.append(kwargs)
        outcome = self._outcomes.pop(0) if self._outcomes else "ok"
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _FakeChat:
    def __init__(self, calls, outcomes):
        self.completions = _FakeCompletions(calls, outcomes)


class _FakeClient:
    def __init__(self, outcomes=None):
        self.calls = []
        self.outcomes = list(outcomes or ["ok"])
        self.with_options_calls = []
        self.chat = _FakeChat(self.calls, self.outcomes)

    def with_options(self, **kwargs):
        self.with_options_calls.append(kwargs)
        return self


def test_retry_loop_uses_original_client_when_decoupling_off(monkeypatch):
    agent = gx10.GX10.__new__(gx10.GX10)
    agent.model = "model"
    agent.messages = []
    agent.max_tokens = 32
    agent.client = _FakeClient()
    agent._turn_gen_errored = False

    monkeypatch.setattr(gx10, "LLM_FIRST_TOKEN_TIMEOUT_S", None)
    monkeypatch.setattr(gx10, "_effective_tools", lambda: [])
    monkeypatch.setattr(gx10.GX10, "_sanitize_tool_call_history", lambda self: None)
    monkeypatch.setattr(gx10.GX10, "_preflight_context", lambda self, think: 32)
    gx10._CANCEL_EVENT.clear()

    assert agent._make_completion(think=False, stream=False) == "ok"
    assert agent.client.with_options_calls == []
    assert len(agent.client.calls) == 1


def _agent(client):
    agent = gx10.GX10.__new__(gx10.GX10)
    agent.model = "model"
    agent.messages = []
    agent.max_tokens = 32
    agent.client = client
    agent._turn_gen_errored = False
    return agent


def _patch_completion_prereqs(monkeypatch):
    monkeypatch.setattr(gx10, "_effective_tools", lambda: [])
    monkeypatch.setattr(gx10.GX10, "_sanitize_tool_call_history", lambda self: None)
    monkeypatch.setattr(gx10.GX10, "_preflight_context", lambda self, think: 32)
    gx10._CANCEL_EVENT.clear()


def test_retry_loop_does_not_amplify_decoupled_timeout(monkeypatch):
    monkeypatch.setattr(gx10, "LLM_FIRST_TOKEN_TIMEOUT_S", 600.0)
    _patch_completion_prereqs(monkeypatch)
    client = _FakeClient([httpx.ReadTimeout("read timed out"), "ok"])
    agent = _agent(client)

    with pytest.raises(httpx.ReadTimeout):
        agent._make_completion(think=False, stream=False)

    assert client.with_options_calls == [{"max_retries": 0}]
    assert len(client.calls) == 1


def test_retry_loop_keeps_one_retry_for_decoupled_non_timeout(monkeypatch):
    monkeypatch.setattr(gx10, "LLM_FIRST_TOKEN_TIMEOUT_S", 600.0)
    _patch_completion_prereqs(monkeypatch)
    client = _FakeClient([ValueError("boom"), "ok"])
    agent = _agent(client)

    assert agent._make_completion(think=False, stream=False) == "ok"
    assert client.with_options_calls == [{"max_retries": 0}, {"max_retries": 0}]
    assert len(client.calls) == 2


def test_retry_loop_uses_original_client_and_retries_timeout_when_coupled(monkeypatch):
    monkeypatch.setattr(gx10, "LLM_FIRST_TOKEN_TIMEOUT_S", None)
    _patch_completion_prereqs(monkeypatch)
    client = _FakeClient([httpx.ReadTimeout("read timed out"), "ok"])
    agent = _agent(client)

    assert agent._make_completion(think=False, stream=False) == "ok"
    assert client.with_options_calls == []
    assert len(client.calls) == 2


def test_finalize_outcome_preserves_errors_and_reports_effective_idle_limit(monkeypatch):
    monkeypatch.setattr(gx10, "TURN_IDLE_TIMEOUT_S", 240.0)
    monkeypatch.setattr(gx10, "LLM_FIRST_TOKEN_TIMEOUT_S", 600.0)
    err = {"kind": "error", "detail": "first-token timeout after 600s"}

    assert gx10._finalize_outcome(err, watchdog_tripped=True, first_token_seen=False) is err
    assert gx10._finalize_outcome({"kind": "abort"}, True, True) == {
        "kind": "stalled",
        "detail": "no progress for 240s",
    }
    assert gx10._finalize_outcome({"kind": "abort"}, True, False) == {
        "kind": "stalled",
        "detail": "no progress for 840s",
    }


def test_generation_error_outcome_names_first_token_timeout(monkeypatch):
    monkeypatch.setattr(gx10, "LLM_FIRST_TOKEN_TIMEOUT_S", 600.0)

    first = gx10._generation_error_outcome(
        stream=True,
        err=httpx.ReadTimeout("read timed out"),
        first_token_seen=False,
    )
    later = gx10._generation_error_outcome(
        stream=True,
        err=httpx.ReadTimeout("read timed out"),
        first_token_seen=True,
    )

    assert first["kind"] == "error"
    assert "first-token timeout" in first["detail"]
    assert "600" in first["detail"]
    assert later["detail"].startswith("API:")


def test_partial_persist_accepts_only_decoupled_watchdog_post_token_clean_content():
    assert gx10._should_persist_partial(
        decoupled=True,
        watchdog_tripped=True,
        first_token_seen=True,
        in_think=False,
    ) is True

    partial = gx10._partial_assistant_message("<think>private</think>\nVisible answer")

    assert partial == {"role": "assistant", "content": "Visible answer"}
    assert "tool_calls" not in partial


def test_partial_assistant_message_drops_unclosed_text_tool_call_tail():
    assert gx10._partial_assistant_message('<think>x</think>ANS<tool_call>{"n"') == {
        "role": "assistant",
        "content": "ANS",
    }


def test_partial_assistant_message_rejects_only_text_tool_call_markup():
    assert gx10._partial_assistant_message('<tool_call>{"a":1}</tool_call>') is None


def test_partial_assistant_message_keeps_visible_answer_after_think_markup():
    assert gx10._partial_assistant_message("<think>t</think>Real answer.") == {
        "role": "assistant",
        "content": "Real answer.",
    }


@pytest.mark.parametrize(
    ("content", "empty"),
    [
        ("", True),
        (None, True),
        ("<think>closed</think>", True),
        ("<think>unclosed reasoning with no close", True),
        ("<think>t</think>Real answer.", False),
        ("Answer <think>x</think>", False),
        ("partial answer then <think>cut", False),
    ],
)
def test_answer_is_empty_handles_closed_and_unclosed_think(content, empty):
    assert gx10._answer_is_empty(content) is empty


def test_should_finalize_truncation_accepts_only_reasoning_runaway():
    assert gx10._should_finalize_truncation(
        flag=True,
        finalized=False,
        tool_calls=[],
        finish_reason="length",
        content="<think>unclosed reasoning",
    ) is True

    assert gx10._should_finalize_truncation(
        flag=True,
        finalized=False,
        tool_calls=[],
        finish_reason="length",
        content="<think>done</think>",
    ) is True


def test_finalize_metrics_accounting_counts_runaway_and_finalize_only_when_enabled():
    perf = {"gens": 0, "prompt": 0, "completion": 0, "wall": 0.0, "last": ""}
    turn = {"gens": 0, "prompt": 0, "completion": 0}
    gen1 = {"prompt_tokens": 11, "completion_tokens": 100, "total": 1.5}
    gen2 = {"prompt_tokens": 7, "completion_tokens": 9, "total": 0.25}

    assert gx10._should_finalize_truncation(
        flag=True,
        finalized=False,
        tool_calls=[],
        finish_reason="length",
        content="<think>runaway",
    ) is True
    assert gx10._accumulate_generation_metrics(perf, turn, gen1) is True
    assert gx10._accumulate_generation_metrics(perf, turn, gen2) is True
    assert perf == {"gens": 2, "prompt": 18, "completion": 109, "wall": 1.75, "last": ""}
    assert turn == {"gens": 2, "prompt": 18, "completion": 109}

    off_perf = {"gens": 0, "prompt": 0, "completion": 0, "wall": 0.0, "last": ""}
    off_turn = {"gens": 0, "prompt": 0, "completion": 0}
    assert gx10._should_finalize_truncation(
        flag=False,
        finalized=False,
        tool_calls=[],
        finish_reason="length",
        content="<think>runaway",
    ) is False
    assert off_perf == {"gens": 0, "prompt": 0, "completion": 0, "wall": 0.0, "last": ""}
    assert off_turn == {"gens": 0, "prompt": 0, "completion": 0}


@pytest.mark.parametrize(
    ("flag", "finalized", "tool_calls", "finish_reason", "content"),
    [
        (False, False, [], "length", "<think>runaway"),
        (True, True, [], "length", "<think>runaway"),
        (True, False, [{"name": "read_file"}], "length", "<think>runaway"),
        (True, False, [], "stop", "<think>runaway"),
        (True, False, [], "length", "<think>t</think>Real answer."),
    ],
)
def test_should_finalize_truncation_rejects_guard_failures(
    flag, finalized, tool_calls, finish_reason, content
):
    assert gx10._should_finalize_truncation(
        flag=flag,
        finalized=finalized,
        tool_calls=tool_calls,
        finish_reason=finish_reason,
        content=content,
    ) is False


@pytest.mark.parametrize(
    ("decoupled", "watchdog_tripped", "first_token_seen", "in_think"),
    [
        (False, True, True, False),
        (True, False, True, False),
        (True, True, False, False),
        (True, True, True, True),
    ],
)
def test_partial_persist_rejects_guard_failures(decoupled, watchdog_tripped, first_token_seen, in_think):
    assert gx10._should_persist_partial(
        decoupled=decoupled,
        watchdog_tripped=watchdog_tripped,
        first_token_seen=first_token_seen,
        in_think=in_think,
    ) is False


def test_partial_persist_rejects_empty_cleaned_content():
    assert gx10._partial_assistant_message("<think>private</think>") is None


def test_partial_persist_rejects_unknown_thinking_state():
    assert gx10._should_persist_partial(
        decoupled=True,
        watchdog_tripped=True,
        first_token_seen=True,
        in_think=None,
    ) is False


def test_generate_resets_first_token_seen_before_non_stream_prefill(monkeypatch):
    agent = gx10.GX10.__new__(gx10.GX10)
    agent.stream = False
    agent._first_token_seen = True
    seen_at_plain_entry = []

    def fake_plain(self, think):
        seen_at_plain_entry.append(self._first_token_seen)
        return "", [], False, None, {}

    monkeypatch.setattr(gx10.GX10, "_generate_plain", fake_plain)

    assert agent._generate(think=False) == ("", [], False, None, {})
    assert seen_at_plain_entry == [False]
