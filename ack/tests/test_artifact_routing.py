"""STATE-Layout Unit B3: artifact routing → the ACTIVE initiative (hybrid layout).

All "file communication" (tasks, handovers, feedback, active.md, archive) routes under
vault/<slug>/ instead of the WORKDIR root — visible artifacts flat, machine plumbing hidden
under <slug>/.work/. Creating ops are fail-closed without an active initiative; background
scanners degrade to no-ops (never crash the daemon). The project root stays clean.
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

import gx10  # noqa: E402

_TASK = {"type": "feature", "priority": "high", "title": "wire it", "description": "do the thing"}


@pytest.fixture(autouse=True)
def _in_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


# ── TaskStore routes to the active initiative ───────────────────
def test_taskstore_writes_under_active_initiative(tmp_path):
    gx10.initiative_new("Routed", "software")
    task = gx10._store().create(dict(_TASK), force=True)
    tid = task["id"]
    assert (tmp_path / "vault" / "routed" / "tasks" / "pending" / f"{tid}.json").is_file()
    assert not (tmp_path / "tasks").exists()       # NOT scattered in the project root


def test_taskstore_failclosed_without_active(tmp_path):
    # no initiative active → create must refuse and write nothing to the root
    with pytest.raises(RuntimeError):
        gx10._store().create(dict(_TASK), force=True)
    assert not (tmp_path / "tasks").exists()


def test_taskstore_list_soft_without_active():
    # background reads degrade to empty, they do not raise
    assert gx10._store().list() == []
    assert gx10._store().get("KGC-1") is None


# ── stage_handover → .work/handovers inbox ────────────────────
def test_stage_handover_lands_in_work_inbox(tmp_path):
    gx10.initiative_new("Staged", "software")
    tid = gx10._store().create(dict(_TASK), force=True)["id"]
    out = gx10._stage_handover(tid, "OPUS", "## Handover\nbody")
    assert "ERROR" not in out
    ho = tmp_path / "vault" / "staged" / ".work" / "handovers" / f"{tid}_OPUS.md"
    assert ho.is_file()
    assert not (tmp_path / "summaries").exists()    # old inbox location gone


def test_stage_handover_failclosed_without_active(tmp_path):
    out = gx10._stage_handover("KGC-1", "OPUS", "## Handover\nbody")
    assert out.startswith("ERROR")                  # fail-closed, clear message
    assert not (tmp_path / "summaries").exists()


# ── full advance round-trip, all under vault/<slug>/ ──────────
def test_advance_round_trip_under_initiative(tmp_path):
    gx10.initiative_new("Flow", "software")
    base = tmp_path / "vault" / "flow"
    store = gx10._store()
    tid = store.create(dict(_TASK), force=True)["id"]
    store.transition(tid, "in_progress")            # projects active.md
    gx10._stage_handover(tid, "OPUS", "## Handover\nbody", set_active=True)
    # the local agent drops feedback into the inbox
    fb = gx10.feedback_dir() / f"{tid}_OPUS-feedback.md"
    fb.parent.mkdir(parents=True, exist_ok=True)
    fb.write_text("## Result\nok", encoding="utf-8")

    out = gx10._advance_pipeline(tid, "OPUS")
    assert "ERROR" not in out

    # task → done; inbox cleared; feedback archived; active.md projected — all under the initiative
    assert (base / "tasks" / "done" / f"{tid}.json").is_file()
    assert not (base / "tasks" / "in_progress" / f"{tid}.json").exists()
    assert not (base / ".work" / "handovers" / f"{tid}_OPUS.md").exists()      # inbox handover deleted
    assert not (base / ".work" / "feedback" / f"{tid}_OPUS-feedback.md").exists()  # inbox feedback consumed
    assert (base / ".work" / "archive" / "feedback" / f"{tid}_OPUS-feedback.md").is_file()
    assert (base / ".work" / "active.md").is_file()
    # project root stays clean: only vault/ + .ironclad/
    assert not (tmp_path / "tasks").exists()
    assert not (tmp_path / "summaries").exists()
    assert not (tmp_path / "reviews").exists()


def test_advance_failclosed_without_active(tmp_path):
    out = gx10._advance_pipeline("KGC-1", "OPUS")
    assert out.startswith("ERROR")


# ── switching initiative switches the whole task view ───────────
def test_switching_initiative_isolates_tasks(tmp_path):
    gx10.initiative_new("Project A", "software")
    gx10._store().create(dict(_TASK), force=True)
    assert len(gx10._store().list("pending")) == 1

    gx10.initiative_new("Project B", "software")       # now active
    assert gx10._store().list("pending") == []        # B's task view is its own (empty)

    gx10.initiative_use("project-a")
    assert len(gx10._store().list("pending")) == 1     # A's task is back in view


# ── autopilot logs are machinery → .ironclad/logs, never a bare root dir ──
def test_autopilot_logs_route_under_state_root(tmp_path, monkeypatch):
    """Regression (DoD #3 counterexample): the autopilot launcher used to mkdir a bare ``logs/`` in
    the WORKDIR root. Logs are engine machinery (subprocess stdout) → they belong under state_root."""
    gx10._apply_config(gx10._code_defaults())
    gx10.STORE = None
    monkeypatch.chdir(tmp_path)
    gx10.initiative_new("Auto", "software")
    tid = gx10._store().create(dict(_TASK), force=True)["id"]
    (gx10.handovers_dir() / f"{tid}_OPUS.md").write_text(
        "---\nto: claude-opus-4-8\n---\nho", encoding="utf-8")

    class _FakeProc:
        pid = 4242
        def poll(self): return 0
        def wait(self, *a, **k): return 0

    monkeypatch.setattr(gx10.subprocess, "Popen", lambda *a, **k: _FakeProc())
    gx10._autopilot_reserve()          # the reconciler pre-reserves the slot before _do_launch
    gx10._do_launch(tid, "OPUS")

    assert (tmp_path / ".ironclad" / "logs" / f"{tid}_OPUS.log").is_file()  # hidden machinery
    assert not (tmp_path / "logs").exists()                                 # NOT scattered in the root
    assert {p.name for p in tmp_path.iterdir()} <= {".ironclad", "vault"}


# ── background scanners are daemon-safe without a initiative ─────
def test_scanners_soft_without_active():
    assert gx10._find_handover("KGC-1") is None
    assert gx10.feedback_dir(soft=True) is None
    assert gx10.handovers_dir(soft=True) is None
    # a reconciler tick with no active initiative must be a clean no-op
    gx10._reconcile_once(gx10._store(), lambda *a, **k: None, {}, set())
