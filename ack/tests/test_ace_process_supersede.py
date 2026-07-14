"""ACE-WIRE (#855 / #863) — Process-SC is SUBSUMED by ACE's reflect→curate, but its TYPED read surface
NEVER silently breaks (the C0 correctness requirement).

ACE supersedes the #602 Process-SC `post_feedback` consumer (no more synchronous typed-lesson write at
completion — the async ReflectionWorker owns the reflection path now). What MUST stay intact is the typed
provider surface Process-SC couples to: `_concrete_lesson_provider` is now DUCK-TYPED, so it returns the
always-on PlaybookStore (which implements `record`/`by_category` over the bullet playbook). Process-SC's
read path (`_process_hint`) therefore keeps working against the new backend.

    python -m pytest ack/tests/test_ace_process_supersede.py -q
"""
from __future__ import annotations

from design_test_support import approve_active_design

import sys
import types
from pathlib import Path

import pytest

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=lambda **kw: object()))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10
import project_registry
from ack import hooks
from ack import lessons as L
from lesson_store import LessonCategory
from playbook_store import PlaybookStore

_TASK = ('{"type":"feature","priority":"high","title":"Build the process feature",'
         '"description":"Build the complete process feature through the validated staging pipeline."}')
_SCOPE = "proj_sc::track::main"


class _FakeAgent:
    def run(self, t): pass
    def save_session(self): pass
    def status(self): return "ok"


def _hard_reset():
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
    monkeypatch.setattr(project_registry, "ironclad_home", lambda: tmp_path)
    monkeypatch.setattr(gx10, "_active_mem_ns", lambda default="": _SCOPE, raising=False)
    yield
    _hard_reset()
    gx10._EFFECTIVE_CFG = saved


def _drive_to_completion(tmp_path, monkeypatch, cfg):
    gx10._apply_config(cfg)
    if gx10._ACE_WORKER is not None:
        gx10._ACE_WORKER.stop()                 # halt the daemon (deterministic — no background drain)
    gx10.STORE = None
    monkeypatch.chdir(tmp_path)
    gx10._dispatch(_FakeAgent(), "initiative new Order Service --type software")
    approve_active_design(gx10)
    gx10._stage_handover(None, "OPUS", "## Handover\nbuild it", task_json=_TASK, force=True)
    tid = gx10._store().list("pending")[0]["id"]
    (gx10.feedback_dir() / f"{tid}_OPUS-feedback.md").write_text(
        "status: done\n\ndone", encoding="utf-8")
    return tid, gx10._advance_pipeline(tid, "OPUS")


def test_concrete_provider_is_the_playbook_duck_typed(tmp_path, monkeypatch):
    gx10._apply_config(gx10._code_defaults())
    prov = gx10._concrete_lesson_provider()
    assert isinstance(prov, PlaybookStore)          # duck-typed: the typed surface resolves to the playbook
    assert prov is L.get_provider()


def test_process_sc_typed_roundtrip_survives_on_the_playbook(tmp_path, monkeypatch):
    gx10._apply_config(gx10._code_defaults())
    prov = gx10._concrete_lesson_provider()
    prov.record(_SCOPE, "the X migration path works", LessonCategory.BEST_KNOWN_PATH)
    # Process-SC's typed read still round-trips (it never silently breaks).
    assert prov.by_category(_SCOPE, LessonCategory.BEST_KNOWN_PATH) == ["the X migration path works"]


def test_process_consumer_is_retired_no_sync_write_on_completion(tmp_path, monkeypatch):
    cfg = gx10._code_defaults()
    cfg["process"]["enabled"] = True            # even with the old flag on, the #602 consumer is gone
    gx10._EFFECTIVE_CFG = cfg
    _tid, out = _drive_to_completion(tmp_path, monkeypatch, cfg)
    assert out.startswith("OK: pipeline advanced")
    # The old Process-SC consumer wrote a typed lesson SYNCHRONOUSLY at completion. ACE submits a Trajectory
    # to the async worker instead (not drained here, no model), so NO synchronous process-lesson was written.
    assert gx10._concrete_lesson_provider().by_category(_SCOPE, LessonCategory.BEST_KNOWN_PATH) == []


def test_process_hint_reads_the_playbook_when_enabled(tmp_path, monkeypatch):
    cfg = gx10._code_defaults()
    cfg["process"]["enabled"] = True
    gx10._EFFECTIVE_CFG = cfg
    gx10._apply_config(cfg)
    gx10._concrete_lesson_provider().record(_SCOPE, "always run the parser check",
                                            LessonCategory.BEST_KNOWN_PATH)
    hint = gx10._process_hint()                 # the Process-SC read path, now sourced from the playbook
    assert "always run the parser check" in hint
