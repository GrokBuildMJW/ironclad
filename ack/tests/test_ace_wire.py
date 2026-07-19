"""ACE-WIRE (#855 / #863) — the engine wiring that makes ACE the ALWAYS-ON loop-intelligence core,
SUPERSEDING the #602 string-lesson + Process-SC `post_feedback` consumers (operator decision 2026-06-30).

Proves through the real `_apply_config` + `_advance_pipeline` wrapper that:
  * ACE registers a PlaybookStore provider unconditionally — there is NO enable flag (always-on);
  * a completed advance SUBMITS a Trajectory to the background ReflectionWorker (off the hot path, never
    inline) and, drained with a model wired, learns the feedback into the scope's playbook;
  * an already-done re-advance does NOT re-submit (the fresh-completion gate);
  * a FOREIGN provider (a plugin's own backend) is never clobbered — ACE steps back (richer-wins);
  * the legacy #602 lesson tree is migrated into the playbook on first wiring;
  * a broken model never breaks the turn (fail-soft).

    python -m pytest ack/tests/test_ace_wire.py -q
"""
from __future__ import annotations

from design_test_support import approve_active_design

import json
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
import project_registry
import gx10
from ack import hooks
from ack import lessons as L
from playbook_store import PlaybookStore

_TASK = ('{"type":"feature","priority":"high","title":"Build the wired feature",'
         '"description":"Build the complete wired feature through the validated staging pipeline."}')


class _FakeAgent:
    def run(self, t): pass
    def save_session(self): pass
    def status(self): return "ok"


class _ForeignProvider:
    """A plugin's own lesson backend (NOT a built-in EngineLessonStore/PlaybookStore)."""

    def __init__(self):
        self.reported = []

    def get_lessons(self, scope, query="", limit=10): return []
    def report_lesson(self, scope, lesson, metadata=None): self.reported.append((scope, lesson))
    def brief(self, scopes, limit=10): return ""


class _RecordingWorker:
    """A deterministic worker stand-in that exposes the submitted Trajectory."""

    def __init__(self):
        self.items = []

    def submit(self, item):
        self.items.append(item)
        return True

    def stop(self):
        pass


def _hard_reset():
    """ACE keeps process-global state (the registered store + the background worker) — quiesce + clear it so
    each test starts clean and no daemon bleeds across tests."""
    if gx10._ACE_WORKER is not None:
        try:
            gx10._ACE_WORKER.stop()
        except Exception:
            pass
    gx10._ACE_WORKER = None
    gx10._ACE_STORE = None
    gx10._ACE_MIGRATED = False
    hooks.clear_hooks()
    L.set_provider(None)


@pytest.fixture(autouse=True)
def _reset(tmp_path, monkeypatch):
    _hard_reset()
    saved = gx10._EFFECTIVE_CFG
    monkeypatch.setattr(project_registry, "ironclad_home", lambda: tmp_path)   # never touch the real install home
    yield
    _hard_reset()
    gx10._EFFECTIVE_CFG = saved


def _drive_full(tmp_path, monkeypatch, *, feedback="status: done\n\ndone feedback", cfg=None,
                feedback_agent="OPUS", requested_agent="OPUS", worker=None):
    """stage → feedback → advance through the real wrapper inside a bound scope ("ns"). Quiesces the worker
    daemon first so a submitted Trajectory stays queued for a deterministic synchronous drain. Returns
    (advance_out, tid)."""
    cfg = cfg or gx10._code_defaults()
    gx10._EFFECTIVE_CFG = cfg
    gx10._apply_config(cfg)
    if gx10._ACE_WORKER is not None:
        gx10._ACE_WORKER.stop()                 # halt the daemon; submissions stay queued (deterministic)
    if worker is not None:
        gx10._ACE_WORKER = worker
    gx10.STORE = None
    monkeypatch.chdir(tmp_path)
    gx10._dispatch(_FakeAgent(), "initiative new Order Service --type software")
    approve_active_design(gx10)
    gx10._stage_handover(None, "OPUS", "## Handover\nbuild it", task_json=_TASK, force=True)
    tid = gx10._store().list("pending")[0]["id"]
    (gx10.feedback_dir() / f"{tid}_{feedback_agent}-feedback.md").write_text(feedback, encoding="utf-8")
    out = gx10._advance_pipeline(tid, requested_agent)
    return out, tid


def test_ace_provider_is_always_on_no_flag(tmp_path, monkeypatch):
    gx10._apply_config(gx10._code_defaults())   # default cfg — NO flag set
    assert isinstance(L.get_provider(), PlaybookStore)              # ACE owns the provider unconditionally
    assert gx10._concrete_lesson_provider() is L.get_provider()     # duck-typed → Process-SC reads it
    assert "post_feedback" in hooks.registered_events()             # the ACE consumer is wired
    assert gx10._ACE_WORKER is not None                             # the background worker is started


def test_completion_submits_trajectory_off_the_hot_path(tmp_path, monkeypatch):
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        out, _tid = _drive_full(tmp_path, monkeypatch)
    assert out.startswith("OK: pipeline advanced")
    assert gx10._ACE_WORKER.pending() == 1                          # submitted, NOT run inline on the turn


def test_mismatched_request_publishes_feedback_agent_and_nonempty_trajectory(tmp_path, monkeypatch):
    cfg = gx10._code_defaults()
    cfg["code_agents"]["pool"].append({
        "provider_id": "cli-codex", "kind": "cli", "agent_id": "CODEX",
        "model": "gpt-codex", "bin": "codex", "cmd_template": "{bin} {prompt}",
        "effort": "high", "permission_mode": "default",
    })
    worker = _RecordingWorker()
    seen = []
    hooks.register_hook("pre_advance", lambda ctx: seen.append(("pre", dict(ctx))))
    hooks.register_hook("post_feedback", lambda ctx: seen.append(("post", dict(ctx))))
    feedback = "status: done\n\nSonnet completed the parser and validation checks."

    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        out, tid = _drive_full(tmp_path, monkeypatch, feedback=feedback, cfg=cfg,
                               feedback_agent="SONNET", requested_agent="CODEX", worker=worker)

    assert "WARNING: requested agent CODEX does not match actual feedback agent SONNET" in out
    assert [(event, ctx["agent"]) for event, ctx in seen] == [("pre", "CODEX"), ("post", "SONNET")]
    assert (gx10.archive_feedback_dir() / f"{tid}_SONNET-feedback.md").exists()
    assert len(worker.items) == 1
    trajectory = worker.items[0]["trajectory"]
    assert trajectory.outcome == "success"
    assert trajectory.steps == [feedback.strip()]


def test_missing_feedback_does_not_submit_or_consume_injected_bullets(tmp_path, monkeypatch):
    cfg = gx10._code_defaults()
    gx10._EFFECTIVE_CFG = cfg
    gx10._apply_config(cfg)
    gx10._ACE_WORKER.stop()
    worker = gx10._ACE_WORKER = _RecordingWorker()
    gx10.STORE = None
    monkeypatch.chdir(tmp_path)

    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        gx10._dispatch(_FakeAgent(), "initiative new Order Service --type software")
        approve_active_design(gx10)
        gx10._stage_handover(None, "OPUS", "## Handover\nbuild it", task_json=_TASK, force=True)
        tid = gx10._store().list("pending")[0]["id"]
        gx10._ace_record_injected(tid, ["b-0", "b-2"])

        missing = gx10._advance_pipeline(tid, "OPUS")
        assert missing.startswith("ERROR: feedback missing")
        assert worker.items == []

        (gx10.feedback_dir() / f"{tid}_OPUS-feedback.md").write_text(
            "status: done\n\nCompleted after feedback arrived.", encoding="utf-8")
        retry = gx10._advance_pipeline(tid, "OPUS")

    assert retry.startswith("OK: pipeline advanced")
    assert len(worker.items) == 1
    assert worker.items[0]["trajectory"].used_bullet_ids == ["b-0", "b-2"]


def test_drain_learns_feedback_into_the_playbook(tmp_path, monkeypatch):
    payload = json.dumps({"insights": [{"content": "validate the parser",
                                        "section": "verification_checklist"}], "ratings": []})
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        out, _tid = _drive_full(tmp_path, monkeypatch)
        gx10._ACE_STORE.set_transports(chat=lambda prompt: payload)  # wire a model, then drain the worker
        assert gx10._ACE_WORKER.process_pending() == 1
        assert "validate the parser" in gx10._ACE_STORE.get_lessons("ns")


def test_already_done_readvance_does_not_resubmit(tmp_path, monkeypatch):
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        out, tid = _drive_full(tmp_path, monkeypatch)
        assert gx10._ACE_WORKER.pending() == 1
        again = gx10._advance_pipeline(tid, "OPUS")                  # already done → no second submission
    assert "already done" in again
    assert gx10._ACE_WORKER.pending() == 1                           # the fresh-completion gate held


def test_foreign_provider_wins_and_ace_steps_back(tmp_path, monkeypatch):
    foreign = _ForeignProvider()
    L.set_provider(foreign)
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        out, _tid = _drive_full(tmp_path, monkeypatch)
    assert out.startswith("OK: pipeline advanced")
    assert L.get_provider() is foreign                              # NOT clobbered (richer-wins)
    assert gx10._ACE_STORE is None                                  # ACE stepped back
    assert "post_feedback" not in hooks.registered_events()         # no ACE consumer, no #602 consumer
    assert foreign.reported == []                                   # a foreign backend is not auto-fed


def test_foreign_playbook_store_is_not_clobbered(tmp_path, monkeypatch):
    # C2 #905: a FOREIGN provider that HAPPENS to be a PlaybookStore instance must NOT be replaced — the
    # faithful supersede check is identity (`current is _ACE_STORE`), not `isinstance(..., PlaybookStore)`.
    foreign = PlaybookStore(tmp_path / "foreign_pb")
    L.set_provider(foreign)
    gx10._apply_config(gx10._code_defaults())
    assert L.get_provider() is foreign                              # untouched (not clobbered by type match)
    assert gx10._ACE_STORE is None                                  # ACE stepped back


def test_apply_ace_threads_ace_top_k(tmp_path, monkeypatch):
    # C2 #905: `ace.top_k` was inert (never threaded to context_for). It now configures the store's cap.
    cfg = dict(gx10._code_defaults()); cfg["ace"] = {"top_k": 3}
    gx10._apply_config(cfg)
    assert isinstance(gx10._ACE_STORE, PlaybookStore) and gx10._ACE_STORE._top_k == 3


def test_reflector_chat_adapter_disables_thinking_with_budget(monkeypatch):
    # #922 (desktop functional test finding): the Reflector call must run thinking-OFF with a real budget — else a reasoning
    # model (qwen3.6-35b) burns the cap on <think>, returns empty content, and the always-on loop learns
    # NOTHING. Assert the adapter sends enable_thinking:False + a >=2048 budget + returns the content.
    cap = {}

    def _create(**kw):
        cap.update(kw)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(
            message=types.SimpleNamespace(content="- [b] a distilled insight #strategies_and_hard_rules"))])

    def _fake_openai(**_kw):
        return types.SimpleNamespace(chat=types.SimpleNamespace(
            completions=types.SimpleNamespace(create=_create)))

    monkeypatch.setattr(gx10, "OpenAI", _fake_openai)
    out = gx10._ace_chat_adapter()("distill reusable insights from this trajectory as JSON")
    assert out.startswith("- [b]")                                            # content is returned
    assert cap["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}   # thinking OFF
    assert cap["max_tokens"] >= 2048                                          # enough budget for the emission


def test_legacy_lesson_tree_is_migrated_on_first_wiring(tmp_path, monkeypatch):
    legacy = tmp_path / "lessons"
    legacy.mkdir(parents=True)
    (legacy / "abc.json").write_text(json.dumps({
        "scope": "ns", "next_seq": 2,
        "lessons": [{"seq": 1, "text": "legacy lesson kept", "category": "best_known_path", "meta": {}}],
    }), encoding="utf-8")
    gx10._apply_config(gx10._code_defaults())                       # ironclad_home → tmp_path (fixture)
    assert "legacy lesson kept" in gx10._ACE_STORE.get_lessons("ns")


def test_failsoft_broken_model_never_breaks_the_turn(tmp_path, monkeypatch):
    def boom(prompt):
        raise RuntimeError("model down")
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        out, _tid = _drive_full(tmp_path, monkeypatch)
        gx10._ACE_STORE.set_transports(chat=boom)
        gx10._ACE_WORKER.process_pending()                          # the worker swallows the error
    assert out.startswith("OK: pipeline advanced")                  # the turn was never affected
    assert gx10._ACE_STORE.get_lessons("ns") == []                  # nothing learned, no crash
