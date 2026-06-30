"""#602 2.3 / #804 — the Lessons completion-write RE-HOMED onto the `post_feedback` Hook-Bus event.

Proves the task-completion lesson write (#601 S14-4) is now driven by a bus consumer
(`gx10._lessons_consumer_hook`) through the real `_advance_pipeline` WRAPPER — not the old inline call in
`_advance_pipeline_impl`:

  * with a provider registered, a completed advance reports the feedback as a scoped lesson and the consumer
    is registered on `post_feedback` — registration is gated on **provider presence** (NOT a flag), exactly
    like the inline write it replaces (so `test_lesson_seam_wiring`'s provider-only setup still works);
  * no provider → no consumer registered, no report (byte-identical no-op);
  * the consumer gates on a FRESH completion → an already-done re-advance does NOT double-report;
  * a raising provider never breaks the advance (fail-soft).

    python -m pytest ack/tests/test_lessons_rehome_wiring.py -q
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
from ack import lessons as L


class _FakeAgent:
    def run(self, t): pass
    def save_session(self): pass
    def status(self): return "ok"


class _StubProvider:
    """Records lesson seam calls (a foreign, non-EngineLessonStore provider)."""

    def __init__(self):
        self.reported = []

    def get_lessons(self, scope, query="", limit=10):
        return []

    def report_lesson(self, scope, lesson, metadata=None):
        self.reported.append((scope, lesson, metadata))

    def brief(self, scopes, limit=10):
        return ""


class _RaisingProvider(_StubProvider):
    def report_lesson(self, scope, lesson, metadata=None):
        raise RuntimeError("boom")


_TASK = '{"type":"feature","priority":"high","title":"Build X","description":"do it"}'


@pytest.fixture(autouse=True)
def _reset():
    hooks.clear_hooks()
    L.set_provider(None)
    yield
    hooks.clear_hooks()
    L.set_provider(None)
    gx10._apply_config(gx10._code_defaults())


def _drive_full(tmp_path, monkeypatch):
    """stage -> feedback -> advance through the real wrapper; returns (advance_out, tid). The provider must be
    set by the caller BEFORE this runs so _apply_config registers the consumer on provider presence."""
    gx10._apply_config(gx10._code_defaults())   # provider present (set by caller) → registers the consumer
    gx10.STORE = None
    monkeypatch.chdir(tmp_path)
    gx10._dispatch(_FakeAgent(), "initiative new Order Service --type software")
    gx10._stage_handover(None, "OPUS", "## Handover\nbuild it", task_json=_TASK, force=True)
    tid = gx10._store().list("pending")[0]["id"]
    (gx10.feedback_dir() / f"{tid}_OPUS-feedback.md").write_text(
        "LESSON: always run the parser check", encoding="utf-8")
    out = gx10._advance_pipeline(tid, "OPUS")
    return out, tid


def test_lessons_consumer_reports_via_advance_wrapper(tmp_path, monkeypatch):
    stub = _StubProvider()
    L.set_provider(stub)                        # provider only — lessons.enabled stays off (matches the seam test)
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        out, tid = _drive_full(tmp_path, monkeypatch)
    assert out.startswith("OK: pipeline advanced")
    assert "post_feedback" in hooks.registered_events()      # the consumer is wired on the bus
    assert len(stub.reported) == 1
    scope, lesson, metadata = stub.reported[0]
    assert scope == "ns"
    assert lesson == "LESSON: always run the parser check"
    assert metadata == {"task_id": tid, "source": "task_completion"}


def test_off_no_provider_no_consumer_byte_identical(tmp_path, monkeypatch):
    # no provider → _apply_lessons_consumer unregisters → no post_feedback hook → byte-identical no-op.
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        out, _tid = _drive_full(tmp_path, monkeypatch)
    assert out.startswith("OK: pipeline advanced")
    assert "post_feedback" not in hooks.registered_events()
    assert L.get_provider() is None


def test_already_done_readvance_does_not_double_report(tmp_path, monkeypatch):
    stub = _StubProvider()
    L.set_provider(stub)
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        out, tid = _drive_full(tmp_path, monkeypatch)
        assert out.startswith("OK: pipeline advanced")
        assert len(stub.reported) == 1
        again = gx10._advance_pipeline(tid, "OPUS")          # already done → "OK: task ... already done"
    assert "already done" in again
    assert len(stub.reported) == 1                           # gate blocked the re-advance: NO second report


def test_failsoft_on_raising_provider(tmp_path, monkeypatch):
    L.set_provider(_RaisingProvider())
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        out, _tid = _drive_full(tmp_path, monkeypatch)
    assert out.startswith("OK: pipeline advanced")           # a raising provider never breaks the advance
