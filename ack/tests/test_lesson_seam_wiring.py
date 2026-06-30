"""S14-4 engine integration tests for the `ack.lessons` seam.

Verifies that gx10's READ site (_stage_handover_impl) injects an advisory lesson
brief into the handover markdown and that the WRITE site (_advance_pipeline_impl)
reports task-completion feedback as a scoped lesson, both gated on a registered
provider and both fail-soft when no provider is present or when the provider raises.
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
    """Stands in for the orchestrator for the deterministic slash-commands."""

    ran = None

    def run(self, t):
        self.ran = t

    def save_session(self):
        pass

    def status(self):
        return "ok"


class StubProvider:
    """Records lesson seam calls and returns a canned brief digest."""

    def __init__(self):
        self.brief_scopes = []
        self.reported = []

    def get_lessons(self, scope, query="", limit=10):
        return []

    def report_lesson(self, scope, lesson, metadata=None):
        self.reported.append((scope, lesson, metadata))

    def brief(self, scopes, limit=10):
        self.brief_scopes.append(list(scopes))
        return "prior lesson A"


class _RaisingProvider:
    """Simulates a broken lesson backend on the hot path."""

    def get_lessons(self, scope, query="", limit=10):
        return []

    def report_lesson(self, scope, lesson, metadata=None):
        raise RuntimeError("boom")

    def brief(self, scopes, limit=10):
        raise RuntimeError("boom")


@pytest.fixture(autouse=True)
def _reset_provider():
    """Keep the process-global lesson provider + the Hook-Bus clean around every test (since #804 a registered
    provider makes `_apply_config` register the `post_feedback` Lessons consumer — clear hooks so it can't bleed
    across tests in this file)."""
    hooks.clear_hooks()
    L.set_provider(None)
    yield
    hooks.clear_hooks()
    L.set_provider(None)


def _lifecycle_stage_only(tmp_path, monkeypatch):
    """Run the engine up to handover staging and return (out, task_id)."""
    gx10._apply_config(gx10._code_defaults())
    gx10.STORE = None
    monkeypatch.chdir(tmp_path)

    gx10._dispatch(_FakeAgent(), "initiative new Order Service --type software")
    out = gx10._stage_handover(
        None,
        "OPUS",
        "## Handover\nbuild it",
        task_json='{"type":"feature","priority":"high","title":"Build X","description":"do it"}',
        force=True,
    )
    tid = gx10._store().list("pending")[0]["id"]
    return out, tid


def _lifecycle_full(tmp_path, monkeypatch):
    """Run the full stage -> feedback -> advance lifecycle and return (stage_out, advance_out, task_id)."""
    gx10._apply_config(gx10._code_defaults())
    gx10.STORE = None
    monkeypatch.chdir(tmp_path)

    gx10._dispatch(_FakeAgent(), "initiative new Order Service --type software")
    stage_out = gx10._stage_handover(
        None,
        "OPUS",
        "## Handover\nbuild it",
        task_json='{"type":"feature","priority":"high","title":"Build X","description":"do it"}',
        force=True,
    )
    tid = gx10._store().list("pending")[0]["id"]
    (gx10.feedback_dir() / f"{tid}_OPUS-feedback.md").write_text(
        "LESSON: always run the parser check", encoding="utf-8"
    )
    advance_out = gx10._advance_pipeline(tid, "OPUS")
    return stage_out, advance_out, tid


def test_read_site_injects_lesson_brief_into_handover(tmp_path, monkeypatch):
    """With a provider registered, staging appends the lesson brief to the handover."""
    stub = StubProvider()
    L.set_provider(stub)

    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        out, tid = _lifecycle_stage_only(tmp_path, monkeypatch)

    assert out.startswith("OK")

    ho_path = gx10.handovers_dir() / f"{tid}_OPUS.md"
    ho_text = ho_path.read_text(encoding="utf-8")
    assert "## Lessons" in ho_text
    assert "prior lesson A" in ho_text
    assert stub.brief_scopes[-1] == ["ns"]


def test_read_site_noop_without_provider(tmp_path, monkeypatch):
    """Without a provider the handover is written unchanged (no Lessons section)."""
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        out, tid = _lifecycle_stage_only(tmp_path, monkeypatch)

    assert out.startswith("OK")

    ho_path = gx10.handovers_dir() / f"{tid}_OPUS.md"
    ho_text = ho_path.read_text(encoding="utf-8")
    assert "## Lessons" not in ho_text
    assert "prior lesson A" not in ho_text


def test_write_site_reports_feedback_as_lesson_with_scope_and_metadata(tmp_path, monkeypatch):
    """Task completion reports the feedback file as a scoped lesson."""
    stub = StubProvider()
    L.set_provider(stub)

    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        stage_out, advance_out, tid = _lifecycle_full(tmp_path, monkeypatch)

    assert stage_out.startswith("OK")
    assert advance_out.startswith("OK")
    assert len(stub.reported) == 1
    scope, lesson, metadata = stub.reported[0]
    assert scope == "ns"
    assert lesson == "LESSON: always run the parser check"
    assert metadata == {"task_id": tid, "source": "task_completion"}


def test_write_site_noop_without_provider(tmp_path, monkeypatch):
    """With no provider, advancing the pipeline is still OK and no lesson is reported."""
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        stage_out, advance_out, _tid = _lifecycle_full(tmp_path, monkeypatch)

    assert stage_out.startswith("OK")
    assert advance_out.startswith("OK")
    assert L.get_provider() is None


def test_write_site_scope_empty_without_bound_context(tmp_path, monkeypatch):
    """When no ProjectContext is bound, the lesson scope falls back to the empty default."""
    stub = StubProvider()
    L.set_provider(stub)

    stage_out, advance_out, _tid = _lifecycle_full(tmp_path, monkeypatch)

    assert stage_out.startswith("OK")
    assert advance_out.startswith("OK")
    assert len(stub.reported) == 1
    assert stub.reported[0][0] == ""


def test_sites_failsoft_on_raising_provider(tmp_path, monkeypatch):
    """A raising provider must not break either the stage or the advance turn."""
    bad = _RaisingProvider()
    L.set_provider(bad)

    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        gx10._apply_config(gx10._code_defaults())
        gx10.STORE = None
        monkeypatch.chdir(tmp_path)

        gx10._dispatch(_FakeAgent(), "initiative new Order Service --type software")
        stage_out = gx10._stage_handover(
            None,
            "OPUS",
            "## Handover\nbuild it",
            task_json='{"type":"feature","priority":"high","title":"Build X","description":"do it"}',
            force=True,
        )
        tid = gx10._store().list("pending")[0]["id"]

        # Capture the staged handover before advance deletes it.
        ho_text = (gx10.handovers_dir() / f"{tid}_OPUS.md").read_text(encoding="utf-8")

        (gx10.feedback_dir() / f"{tid}_OPUS-feedback.md").write_text(
            "LESSON: always run the parser check", encoding="utf-8"
        )
        advance_out = gx10._advance_pipeline(tid, "OPUS")

    assert stage_out.startswith("OK")
    assert advance_out.startswith("OK")
    assert "## Lessons" not in ho_text
