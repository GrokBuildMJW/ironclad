"""Engine integration tests for the `ack.lessons` seam under ACE (epic #855 ACE-WIRE / #863).

The READ site (`_stage_handover_impl`) injects an advisory lesson context into the handover markdown; the
WRITE side is now ACE's `post_feedback` consumer (submit-a-Trajectory, tested in `test_ace_wire`). This
file pins the SEAM contract that survives the ACE supersede:

  * a FOREIGN provider (no `context_for`) still gets its string `brief` injected (back-compat);
  * the always-on ACE PlaybookStore is injected via its query-aware `context_for` (the 32k-safe read) — and
    an empty playbook injects nothing;
  * a foreign provider is NOT auto-fed completion feedback (ACE steps back / richer-wins);
  * every site is fail-soft — a raising provider never breaks the stage or the advance.
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
import project_registry
import gx10
from ack import hooks
from ack import lessons as L
from playbook_store import PlaybookStore

_TASK = '{"type":"feature","priority":"high","title":"Build X","description":"do it"}'


class _FakeAgent:
    def run(self, t): pass
    def save_session(self): pass
    def status(self): return "ok"


class StubProvider:
    """A FOREIGN string provider (no `context_for`) — records seam calls, returns a canned brief."""

    def __init__(self):
        self.reported = []

    def get_lessons(self, scope, query="", limit=10): return []
    def report_lesson(self, scope, lesson, metadata=None): self.reported.append((scope, lesson, metadata))
    def brief(self, scopes, limit=10): return "prior lesson A"


class _RaisingProvider:
    def get_lessons(self, scope, query="", limit=10): return []
    def report_lesson(self, scope, lesson, metadata=None): raise RuntimeError("boom")
    def brief(self, scopes, limit=10): raise RuntimeError("boom")


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
    yield
    _hard_reset()
    gx10._EFFECTIVE_CFG = saved


def _stage_only(tmp_path, monkeypatch):
    gx10._apply_config(gx10._code_defaults())
    gx10.STORE = None
    monkeypatch.chdir(tmp_path)
    gx10._dispatch(_FakeAgent(), "initiative new Order Service --type software")
    gx10._stage_handover(None, "OPUS", "## Handover\nbuild it", task_json=_TASK, force=True)
    return gx10._store().list("pending")[0]["id"]


def _advance_full(tmp_path, monkeypatch):
    tid = _stage_only(tmp_path, monkeypatch)
    (gx10.feedback_dir() / f"{tid}_OPUS-feedback.md").write_text(
        "LESSON: always run the parser check", encoding="utf-8")
    return tid, gx10._advance_pipeline(tid, "OPUS")


# ─── READ site ───────────────────────────────────────────────────────────────────────────────────────
def test_read_site_injects_foreign_provider_brief(tmp_path, monkeypatch):
    """A foreign provider (no `context_for`) still gets its string `brief` injected (back-compat fallback)."""
    L.set_provider(StubProvider())
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        tid = _stage_only(tmp_path, monkeypatch)
    ho = (gx10.handovers_dir() / f"{tid}_OPUS.md").read_text(encoding="utf-8")
    assert "## Lessons" in ho and "prior lesson A" in ho
    assert L.get_provider().__class__.__name__ == "StubProvider"   # foreign provider kept (richer-wins)


def test_read_site_empty_ace_playbook_injects_nothing(tmp_path, monkeypatch):
    """With the always-on ACE provider but an empty playbook, no Lessons section is appended."""
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        tid = _stage_only(tmp_path, monkeypatch)
    ho = (gx10.handovers_dir() / f"{tid}_OPUS.md").read_text(encoding="utf-8")
    assert isinstance(L.get_provider(), PlaybookStore) and "## Lessons" not in ho


def test_read_site_uses_ace_context_for_when_seeded(tmp_path, monkeypatch):
    """A seeded ACE playbook is injected through the query-aware `context_for` read."""
    gx10._apply_config(gx10._code_defaults())
    gx10._ACE_STORE.report_lesson("ns", "validate the parser output before commit")
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        tid = _stage_only(tmp_path, monkeypatch)
    ho = (gx10.handovers_dir() / f"{tid}_OPUS.md").read_text(encoding="utf-8")
    assert "## Lessons" in ho and "validate the parser output before commit" in ho


# ─── WRITE side (ACE supersede) ───────────────────────────────────────────────────────────────────────
def test_write_site_foreign_provider_not_auto_reported(tmp_path, monkeypatch):
    """A foreign backend is NOT auto-fed completion feedback — ACE steps back (richer-wins)."""
    stub = StubProvider()
    L.set_provider(stub)
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        _tid, advance_out = _advance_full(tmp_path, monkeypatch)
    assert advance_out.startswith("OK")
    assert stub.reported == [] and L.get_provider() is stub


def test_write_site_ace_owns_provider_by_default(tmp_path, monkeypatch):
    """With no foreign provider, ACE owns the seam: the provider is the always-on PlaybookStore."""
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        _tid, advance_out = _advance_full(tmp_path, monkeypatch)
    assert advance_out.startswith("OK")
    assert isinstance(L.get_provider(), PlaybookStore)


def test_sites_failsoft_on_raising_foreign_provider(tmp_path, monkeypatch):
    """A raising foreign provider must not break either the stage or the advance."""
    L.set_provider(_RaisingProvider())
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        tid = _stage_only(tmp_path, monkeypatch)
        ho = (gx10.handovers_dir() / f"{tid}_OPUS.md").read_text(encoding="utf-8")
        (gx10.feedback_dir() / f"{tid}_OPUS-feedback.md").write_text("LESSON: x", encoding="utf-8")
        advance_out = gx10._advance_pipeline(tid, "OPUS")
    assert advance_out.startswith("OK") and "## Lessons" not in ho
