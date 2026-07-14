"""Epic #1130 / S1 (#1131) — Guard 1 core: no turn can silently hold the agent lock forever.

Three fail-soft mechanisms so a stalled/wedged turn cannot silently block the orchestrator:
  1. a per-request LLM timeout on every OpenAI client (agent + ACE reflector; workers/MPR reuse the agent's);
  2. tool-loop cancel-honouring that never leaves an orphan `tool_calls` message (a vLLM 400 otherwise);
  3. a bounded agent-lock acquire → 503 "busy" instead of an indefinite block behind a wedged turn.
"""
from __future__ import annotations

import sys
import types
import threading
from pathlib import Path

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=object))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402


# ── 1) per-request LLM timeout on the client ──────────────────────────────────
def test_agent_client_gets_request_timeout_and_retries(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    cap = {}
    monkeypatch.setattr(gx10, "OpenAI", lambda **kw: cap.update(kw) or object())
    gx10.GX10(base_url="http://x/v1", api_key="k", model="m", prompt_path="")
    timeout = cap.get("timeout")
    assert timeout.connect == gx10.LLM_CONNECT_TIMEOUT_S
    assert timeout.read == gx10.LLM_FIRST_TOKEN_TIMEOUT_S
    assert timeout.write == gx10.LLM_REQUEST_TIMEOUT_S
    assert timeout.pool == gx10.LLM_REQUEST_TIMEOUT_S
    assert cap.get("max_retries") == gx10.LLM_MAX_RETRIES
    assert all(value is not None and value > 0 for value in (
        timeout.connect, timeout.read, timeout.write, timeout.pool,
    ))


def test_llm_timeout_config_default_and_env_override(monkeypatch):
    d = gx10._code_defaults()["connection"]
    assert d["request_timeout_s"] == gx10.LLM_REQUEST_TIMEOUT_S and "max_retries" in d
    saved = (gx10.LLM_REQUEST_TIMEOUT_S, gx10.LLM_MAX_RETRIES)
    try:
        monkeypatch.setenv("GX10_LLM_TIMEOUT_S", "55")
        monkeypatch.setenv("GX10_LLM_MAX_RETRIES", "0")
        gx10._apply_config(gx10._apply_env(gx10._code_defaults()))
        assert gx10.LLM_REQUEST_TIMEOUT_S == 55.0 and gx10.LLM_MAX_RETRIES == 0
    finally:
        gx10.LLM_REQUEST_TIMEOUT_S, gx10.LLM_MAX_RETRIES = saved   # no cross-test bleed of the module global


# ── 2) tool-loop cancel-honouring: no orphan tool_calls ───────────────────────
def test_tool_loop_cancel_leaves_no_orphan_tool_calls(monkeypatch, tmp_path):
    # A cancel that lands mid-tool-round must answer EVERY tool_call in the assistant message (else the next
    # send is a hard vLLM 400 for an unanswered tool_call). Drive one round of 2 tool_calls, trip cancel as a
    # side effect of generation so the tool loop sees it set, and assert the round is closed cleanly.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(gx10, "OpenAI", lambda **kw: object())
    g = gx10.GX10(base_url="http://x/v1", api_key="k", model="m", prompt_path="")
    g.messages = [{"role": "system", "content": "SYS"}]
    gx10._CANCEL_EVENT.clear()
    monkeypatch.setattr(g, "_classify_thinking", lambda _u: False)

    calls = {"n": 0}

    def fake_generate(think):
        calls["n"] += 1
        gx10._CANCEL_EVENT.set()                                   # cancel lands after generation, before the tools
        tcs = [{"id": "a", "name": "read_file", "arguments": "{}"},
               {"id": "b", "name": "read_file", "arguments": "{}"}]
        return ("", tcs, False, None, {})
    monkeypatch.setattr(g, "_generate", fake_generate)

    try:
        g.run("do stuff")
    finally:
        gx10._CANCEL_EVENT.clear()

    assistant = [m for m in g.messages if m.get("role") == "assistant" and m.get("tool_calls")]
    assert len(assistant) == 1
    tc_ids = {tc["id"] for tc in assistant[0]["tool_calls"]}
    tool_ids = {m["tool_call_id"] for m in g.messages if m.get("role") == "tool"}
    assert tc_ids == tool_ids and tc_ids == {"a", "b"}            # every tool_call answered → no orphan
    assert all(m["content"] == "ERROR: cancelled"
               for m in g.messages if m.get("role") == "tool")
    assert calls["n"] == 1                                        # stopped after one round, no run-away loop


# ── 3) bounded agent-lock acquire → 503 instead of an indefinite block ─────────
def test_agent_lock_has_bounded_acquire_timeout():
    import server  # noqa: E402 — engine is on sys.path; import is side-effect-free (server starts under __main__)
    assert isinstance(server._AGENT_LOCK_TIMEOUT_S, float) and server._AGENT_LOCK_TIMEOUT_S > 0
    lock = server._AGENT_LOCK
    assert isinstance(lock, type(threading.Lock()))
    assert lock.acquire()
    try:
        # the mechanism the /chat 503 path relies on: a held lock times out the waiter, never blocks forever
        assert lock.acquire(timeout=0.1) is False
    finally:
        lock.release()


def test_agent_lock_timeout_env_override(monkeypatch):
    # deploy-tunable without a code edit (same as the LLM timeout) — proves the knob is wired to the env
    monkeypatch.setenv("GX10_AGENT_LOCK_TIMEOUT_S", "7.5")
    import importlib
    import server
    importlib.reload(server)
    try:
        assert server._AGENT_LOCK_TIMEOUT_S == 7.5
    finally:
        monkeypatch.delenv("GX10_AGENT_LOCK_TIMEOUT_S", raising=False)
        importlib.reload(server)                                   # restore module-level default for other tests
