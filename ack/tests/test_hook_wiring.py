"""#602 2.0 / #690 — engine integration tests for the Hook-Bus publish points.

Proves the engine actually PUBLISHES the loop-boundary events (not just that the bus module works):
the handover/advance wrappers fire ``pre_handover`` / ``post_handover`` / ``pre_advance`` /
``post_feedback`` (outside the vault lock), and the agent loop fires ``pre_turn`` / ``post_generate``.
Also asserts the **byte-identical default**: with no hook registered the lifecycle is unaffected.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=lambda **kw: object()))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import project_context as pc
from project_context import ProjectContext
import gx10
from ack import hooks


@pytest.fixture(autouse=True)
def _clean_bus():
    hooks.clear_hooks()
    yield
    hooks.clear_hooks()


def _recorder():
    """A recording hook factory: returns (events_list, make(event_name))."""
    seen = []

    def make(event):
        def _hook(ctx):
            seen.append((event, ctx))
        return _hook

    return seen, make


# ─── lifecycle events: pre/post_handover + pre_advance/post_feedback (proven harness) ────────────────
def _lifecycle_full(tmp_path, monkeypatch):
    gx10._apply_config(gx10._code_defaults())
    gx10.STORE = None
    monkeypatch.chdir(tmp_path)
    gx10._dispatch(types.SimpleNamespace(run=lambda t: None, save_session=lambda: None,
                                         status=lambda: "ok"),
                   "initiative new Order Service --type software")
    stage_out = gx10._stage_handover(
        None, "OPUS", "## Handover\nbuild it",
        task_json='{"type":"feature","priority":"high","title":"Build X","description":"do it"}',
        force=True,
    )
    tid = gx10._store().list("pending")[0]["id"]
    (gx10.feedback_dir() / f"{tid}_OPUS-feedback.md").write_text("LESSON: x", encoding="utf-8")
    advance_out = gx10._advance_pipeline(tid, "OPUS")
    return stage_out, advance_out, tid


def test_handover_and_advance_publish_boundary_events(tmp_path, monkeypatch):
    seen, make = _recorder()
    for ev in ("pre_handover", "post_handover", "pre_advance", "post_feedback"):
        hooks.register_hook(ev, make(ev))

    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        stage_out, advance_out, tid = _lifecycle_full(tmp_path, monkeypatch)

    assert stage_out.startswith("OK")
    assert advance_out.startswith("OK")
    fired = {ev for ev, _ in seen}
    assert fired == {"pre_handover", "post_handover", "pre_advance", "post_feedback"}

    by_event = {ev: ctx for ev, ctx in seen}
    assert by_event["pre_handover"]["agent"] == "OPUS"
    assert by_event["post_handover"]["result"].startswith("OK")
    assert by_event["pre_advance"]["task_id"] == tid
    assert by_event["post_feedback"]["task_id"] == tid
    assert by_event["post_feedback"]["result"].startswith("OK")


def test_lifecycle_byte_identical_with_no_hooks(tmp_path, monkeypatch):
    # No hook registered → the bus is an O(1) no-op; the lifecycle must succeed unchanged.
    assert hooks.registered_events() == ()
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        stage_out, advance_out, _tid = _lifecycle_full(tmp_path, monkeypatch)
    assert stage_out.startswith("OK")
    assert advance_out.startswith("OK")


def test_failing_hook_does_not_break_the_lifecycle(tmp_path, monkeypatch):
    def boom(ctx):
        raise RuntimeError("hook boom")

    for ev in ("pre_handover", "post_handover", "pre_advance", "post_feedback"):
        hooks.register_hook(ev, boom)

    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        stage_out, advance_out, _tid = _lifecycle_full(tmp_path, monkeypatch)
    assert stage_out.startswith("OK")    # fail-soft: a raising hook never breaks the turn
    assert advance_out.startswith("OK")


# ─── agent-loop events: pre_turn + post_generate (the Verifier feed site) ────────────────────────────
def _mk_agent(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(gx10, "OpenAI", lambda **kw: object())
    monkeypatch.setattr(gx10, "_MEMORY", None)
    monkeypatch.setattr(gx10, "RAG_ENABLED", False)
    monkeypatch.setattr(gx10, "_WARM", None)
    g = gx10.GX10(base_url="http://x/v1", api_key="k", model="m", prompt_path="")
    g.messages = [{"role": "system", "content": "SYS"}]
    return g


def test_run_publishes_pre_turn_and_post_generate(monkeypatch, tmp_path):
    seen, make = _recorder()
    hooks.register_hook("pre_turn", make("pre_turn"))
    hooks.register_hook("post_generate", make("post_generate"))

    g = _mk_agent(monkeypatch, tmp_path)
    # Stub the model: one generation, no tool calls → the loop returns right after post_generate.
    monkeypatch.setattr(g, "_generate", lambda think: ("done", [], False, None, {}))
    g.run("hello world")

    fired = {ev for ev, _ in seen}
    assert fired == {"pre_turn", "post_generate"}
    by_event = {ev: ctx for ev, ctx in seen}
    assert by_event["pre_turn"]["user_input"] == "hello world"
    assert by_event["post_generate"]["content"] == "done"
    assert by_event["post_generate"]["tool_calls"] == []


def test_run_byte_identical_with_no_hooks(monkeypatch, tmp_path):
    g = _mk_agent(monkeypatch, tmp_path)
    monkeypatch.setattr(g, "_generate", lambda think: ("done", [], False, None, {}))
    assert hooks.registered_events() == ()
    g.run("hello")                       # must not raise; no hooks → no-op
    assert g.last_response == "done"
