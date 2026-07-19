"""#1225 (S3) — per-turn AUTHORITATIVE steering-state injection into the orchestrator model.

The builder reads active project · unit · artifact root · lifecycle stage · pending/in_progress counts · watcher/autopilot
from the SAME globals the plumbing acts on, folds a compact block onto the user turn (after the stable system
prefix, KV-cache-safe), and returns "" when nothing is bound so a plain-chat turn stays byte-identical. It
must never raise. These tests cover the builder (state → string) and the run() injection, modelled on
``test_context_rag.py``.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402
import project_context as pc  # noqa: E402
from project_context import ProjectContext  # noqa: E402


class _FakeStore:
    """Minimal TaskStore stand-in: .list(status) → a canned list of the given length."""

    def __init__(self, pending: int = 0, in_progress: int = 0):
        self._counts = {"pending": pending, "in_progress": in_progress}

    def list(self, status=None):
        return [{"id": f"KGC-{i}"} for i in range(1, self._counts.get(status, 0) + 1)]


class _FakeRegistry:
    def has(self, agent):
        return agent in {"OPUS", "SONNET"}


def _bind(monkeypatch, *, project="dev1test", status="ok", unit="my-unit",
          pending=1, in_progress=2, watcher=True, autopilot=False, autoplan=False):
    """Monkeypatch every state source so the builder sees a fully-bound state."""
    monkeypatch.setattr(gx10, "registry_health",
                        lambda: {"status": status, "active_project": project, "home": None})
    monkeypatch.setattr(gx10, "active_slug", lambda: unit)
    monkeypatch.setattr(gx10, "_store", lambda: _FakeStore(pending, in_progress))
    monkeypatch.setattr(gx10, "_WATCHER_ENABLED", watcher)
    monkeypatch.setattr(gx10, "AUTOPILOT_ENABLED", autopilot)
    monkeypatch.setattr(gx10, "AUTOPILOT_AUTOPLAN", autoplan)


def _feedback_paths(monkeypatch, tmp_path):
    live = tmp_path / "feedback"
    archive = tmp_path / "archive" / "feedback"
    live.mkdir(parents=True)
    archive.mkdir(parents=True)
    monkeypatch.setattr(gx10, "feedback_dir", lambda: live)
    monkeypatch.setattr(gx10, "archive_feedback_dir", lambda: archive)
    monkeypatch.setattr(gx10, "_code_agent_registry", lambda: _FakeRegistry())
    return live, archive


# ── builder: state → string ──────────────────────────────────────────────────

def test_block_full_state(monkeypatch):
    _bind(monkeypatch)
    block = gx10._steering_state_block()
    assert block.startswith(gx10._STEERING_MARKER)
    assert "active project: dev1test" in block
    assert "active unit (initiative): my-unit" in block
    assert "1 pending" in block and "2 in_progress" in block
    assert "watcher: on" in block and "autopilot: off" in block
    assert "do NOT invent a vault path" in block


def test_block_artifacts_uses_resolved_root_adjacent_to_active_unit(monkeypatch, tmp_path):
    _bind(monkeypatch, project="alpha", unit="unit-a")
    vault = tmp_path / "configured-vault"
    resolved_root = vault / "engine-resolved-unit"
    monkeypatch.setattr(gx10, "vault_root", lambda: vault)
    monkeypatch.setattr(gx10, "artifact_root_soft", lambda: resolved_root)

    block = gx10._steering_state_block()

    expected_path = gx10._display_doc_path(
        gx10.artifact_root_soft().relative_to(gx10.vault_root()).as_posix()
    ).rstrip("/") + "/"
    lines = block.splitlines()
    unit_index = lines.index("- active unit (initiative): unit-a")
    assert lines[unit_index + 1] == (
        f"- artifacts: {expected_path}  "
        "(proposals/ decisions/ tasks/ · plumbing under .work/)"
    )


def test_block_empty_when_nothing_bound(monkeypatch):
    # no project AND no unit → "" so the plain-chat / unisolated turn is byte-identical
    _bind(monkeypatch, project=None, status="unisolated", unit=None)
    assert gx10._steering_state_block() == ""


def test_block_unisolated_but_unit_active(monkeypatch):
    _bind(monkeypatch, project=None, status="unisolated", unit="u")
    block = gx10._steering_state_block()
    assert block  # present because a unit is active
    assert "un-isolated" in block
    assert "active unit (initiative): u" in block


def test_block_artifacts_uses_unit_root_when_project_id_differs(monkeypatch, tmp_path):
    _bind(monkeypatch, project="testiron", unit="main")
    resolved_root = tmp_path / "vault" / "main"
    monkeypatch.setattr(gx10, "artifact_root_soft", lambda: resolved_root)

    with pc.use(ProjectContext("testiron", str(tmp_path), "test-ns")):
        block = gx10._steering_state_block()

    assert "- artifacts: vault/main/  " in block
    assert "- artifacts: vault/testiron/" not in block


def test_block_artifacts_is_failsoft(monkeypatch):
    _bind(monkeypatch)

    def _boom():
        raise RuntimeError("artifact root unavailable")

    monkeypatch.setattr(gx10, "artifact_root_soft", _boom)
    block = gx10._steering_state_block()
    assert block.startswith(gx10._STEERING_MARKER)
    assert "- artifacts:" not in block


def test_block_failsoft_when_store_raises(monkeypatch):
    _bind(monkeypatch)

    def _boom():
        raise RuntimeError("store down")

    monkeypatch.setattr(gx10, "_store", _boom)
    block = gx10._steering_state_block()          # must NOT raise
    assert block.startswith(gx10._STEERING_MARKER)
    assert "0 pending" in block and "0 in_progress" in block


def test_block_failsoft_when_active_slug_raises(monkeypatch):
    _bind(monkeypatch)

    def _boom():
        raise RuntimeError("slug read failed")

    monkeypatch.setattr(gx10, "active_slug", _boom)
    # a hint must never break a turn → the blanket guard returns ""
    assert gx10._steering_state_block() == ""


def test_block_stage_from_cached_graph(monkeypatch, tmp_path):
    _bind(monkeypatch, unit="my-unit")
    monkeypatch.setattr(gx10, "vault_root", lambda: tmp_path)
    unit_dir = tmp_path / "my-unit"
    unit_dir.mkdir()
    graph = unit_dir / gx10.GRAPH_FILENAME
    graph.write_text(json.dumps({"lifecycle": {"current": "spec"}}), encoding="utf-8")
    assert "lifecycle stage: spec" in gx10._steering_state_block()
    graph.unlink()                                # no cached projection → stage line omitted
    assert "lifecycle stage:" not in gx10._steering_state_block()


def test_block_guided_feedback_ready_is_byte_identical(monkeypatch, tmp_path):
    _bind(monkeypatch, unit=None, pending=0, in_progress=1, watcher=False)
    live, _archive = _feedback_paths(monkeypatch, tmp_path)
    (live / "KGC-1_SONNET-feedback.md").write_text("status: done\n", encoding="utf-8")
    monkeypatch.setattr(gx10, "_select_next_unit", lambda store: (None, [], 0))

    assert gx10._steering_state_block() == "\n".join([
        gx10._STEERING_MARKER,
        "- active project: dev1test",
        "- active unit (initiative): (none — no vault/<slug> unit is active)",
        "- tasks: 0 pending · 1 in_progress",
        "- feedback ready: KGC-1 (SONNET) — coder feedback is waiting in the inbox; call "
        "advance_pipeline for it (it fail-closes unless the feedback reports done). "
        "Do NOT launch_coder again.",
        "- watcher: off · autopilot: off · continuation: off  [auto: GUIDED]",
        "Trust these fields over any filesystem probe; do NOT invent a vault path.",
    ])


def test_block_guided_next_open_unit_is_byte_identical(monkeypatch, tmp_path):
    _bind(monkeypatch, unit=None, pending=1, in_progress=0, watcher=False)
    _feedback_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(gx10, "_select_next_unit", lambda store: (
        {"id": "KGC-2", "title": "Next unit", "parent": "KGC"}, [], 1))

    assert gx10._steering_state_block() == "\n".join([
        gx10._STEERING_MARKER,
        "- active project: dev1test",
        "- active unit (initiative): (none — no vault/<slug> unit is active)",
        "- tasks: 1 pending · 0 in_progress",
        "- next open unit: KGC-2 ('Next unit') under epic KGC — stage its handover via "
        "stage_handover (task_id, no task_json); /auto on drains all open units automatically.",
        "- watcher: off · autopilot: off · continuation: off  [auto: GUIDED]",
        "Trust these fields over any filesystem probe; do NOT invent a vault path.",
    ])


def test_block_full_reports_automated_legs_without_operator_imperatives(monkeypatch, tmp_path):
    _bind(monkeypatch, unit=None, pending=1, in_progress=1,
          watcher=True, autopilot=True, autoplan=True)
    live, _archive = _feedback_paths(monkeypatch, tmp_path)
    (live / "KGC-1_SONNET-feedback.md").write_text("status: done\n", encoding="utf-8")
    monkeypatch.setattr(gx10, "_select_next_unit", lambda store: (
        {"id": "KGC-2", "title": "Next unit"}, [], 1))

    block = gx10._steering_state_block()

    assert "feedback ready: KGC-1 (SONNET)" in block
    assert "the watcher is responsible for advancing it" in block
    assert "next open unit: KGC-2 ('Next unit')" in block
    assert "the continuation owns staging its handover" in block
    assert "`/auto off` returns staging to the operator" in block
    assert "advance_pipeline" not in block
    assert "stage_handover" not in block
    assert "/auto on" not in block
    assert "is advancing" not in block
    assert "is staging" not in block


def test_block_mixed_gates_each_automation_leg_independently(monkeypatch, tmp_path):
    _bind(monkeypatch, unit=None, pending=1, in_progress=1,
          watcher=True, autoplan=False)
    live, _archive = _feedback_paths(monkeypatch, tmp_path)
    (live / "KGC-1_OPUS-feedback.md").write_text("status: done\n", encoding="utf-8")
    monkeypatch.setattr(gx10, "_select_next_unit", lambda store: (
        {"id": "KGC-2", "title": "Next unit"}, [], 1))

    block = gx10._steering_state_block()

    assert "the watcher is responsible for advancing it" in block
    assert "advance_pipeline" not in block
    assert "stage its handover via stage_handover" in block
    assert "/auto on drains all open units automatically" in block
    assert "is advancing" not in block


def test_block_mixed_gates_the_inverse_automation_legs(monkeypatch, tmp_path):
    _bind(monkeypatch, unit=None, pending=1, in_progress=1,
          watcher=False, autoplan=True)
    live, _archive = _feedback_paths(monkeypatch, tmp_path)
    (live / "KGC-1_OPUS-feedback.md").write_text("status: done\n", encoding="utf-8")
    monkeypatch.setattr(gx10, "_select_next_unit", lambda store: (
        {"id": "KGC-2", "title": "Next unit"}, [], 1))

    block = gx10._steering_state_block()

    assert "call advance_pipeline for it" in block
    assert "the continuation owns staging its handover" in block
    assert "`/auto off` returns staging to the operator" in block
    assert "stage_handover" not in block
    assert "/auto on" not in block
    assert "is staging" not in block


def test_block_without_feedback_is_byte_identical(monkeypatch, tmp_path):
    _bind(monkeypatch, unit=None, pending=1, in_progress=1, watcher=False, autopilot=False)
    _feedback_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(gx10, "AUTOPILOT_AUTOPLAN", False)
    monkeypatch.setattr(gx10, "_select_next_unit", lambda store: (None, [], 0))

    assert gx10._steering_state_block() == "\n".join([
        gx10._STEERING_MARKER,
        "- active project: dev1test",
        "- active unit (initiative): (none — no vault/<slug> unit is active)",
        "- tasks: 1 pending · 1 in_progress",
        "- watcher: off · autopilot: off · continuation: off  [auto: GUIDED]",
        "Trust these fields over any filesystem probe; do NOT invent a vault path.",
    ])


def test_block_ignores_feedback_from_unconfigured_agent(monkeypatch, tmp_path):
    _bind(monkeypatch, in_progress=1)
    live, _archive = _feedback_paths(monkeypatch, tmp_path)
    (live / "KGC-1_UNKNOWN-feedback.md").write_text("status: done\n", encoding="utf-8")

    assert "feedback ready:" not in gx10._steering_state_block()


def test_block_ignores_archived_only_feedback(monkeypatch, tmp_path):
    _bind(monkeypatch, in_progress=1)
    _live, archive = _feedback_paths(monkeypatch, tmp_path)
    (archive / "KGC-1_OPUS-feedback.md").write_text("status: done\n", encoding="utf-8")

    assert "feedback ready:" not in gx10._steering_state_block()


def test_feedback_ready_precedes_next_open_unit_recommendation(monkeypatch, tmp_path):
    _bind(monkeypatch, pending=1, in_progress=1)
    live, _archive = _feedback_paths(monkeypatch, tmp_path)
    (live / "KGC-1_OPUS-feedback.md").write_text("status: done\n", encoding="utf-8")
    monkeypatch.setattr(gx10, "_select_next_unit", lambda store: (
        {"id": "KGC-2", "title": "Next unit"}, [], 1))

    block = gx10._steering_state_block()

    assert block.index("- feedback ready:") < block.index("- next open unit:")


def test_block_surfaces_idle_continuation_recovery(monkeypatch, tmp_path):
    _bind(monkeypatch, unit=None, pending=1, in_progress=0,
          watcher=True, autopilot=True, autoplan=True)
    _feedback_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(gx10, "_find_handover", lambda tid: None)
    monkeypatch.setattr(gx10, "_select_next_unit", lambda store: (
        {"id": "KGC-3", "title": "Eligible unit"}, [], 1))
    gx10._CONTINUATION_AUTHORING.add("KGC-3")
    gx10._set_automation_notice("continuation authoring for KGC-3 failed; the heartbeat will retry")

    block = gx10._steering_state_block()

    assert "automation notice: continuation authoring for KGC-3 failed" in block
    assert "continuation stall: KGC-3 is eligible with nothing in flight" in block
    assert "recovery authoring turn is queued/in flight" in block


def test_block_stall_agrees_with_exhausted_recovery_notice(monkeypatch, tmp_path):
    _bind(monkeypatch, unit=None, pending=1, in_progress=0,
          watcher=True, autopilot=True, autoplan=True)
    _feedback_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(gx10, "_find_handover", lambda tid: None)
    unit = {"id": "KGC-3", "title": "Eligible unit"}
    monkeypatch.setattr(gx10, "_select_next_unit", lambda store: (unit, [], 1))
    gx10._CONTINUATION_RECOVERY_ATTEMPTS["KGC-3"] = gx10._CONTINUATION_RECOVERY_MAX_ATTEMPTS
    assert gx10._enqueue_next_unit(None, unit, lambda _item: None, recovery=True) is False

    block = gx10._steering_state_block()

    assert "automation notice: continuation authoring for KGC-3 stopped after 3 recovery attempts" in block
    stall = next(line for line in block.splitlines() if line.startswith("- continuation stall:"))
    assert "automatic recovery is exhausted" in stall
    assert "stage its handover via `stage_handover`" in stall
    assert "will re-fire" not in stall


# ── injection into run() (modelled on test_context_rag.test_run_flag_*) ───────

def _mk_agent(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(gx10, "OpenAI", lambda **kw: object())
    g = gx10.GX10(base_url="http://x/v1", api_key="k", model="m", prompt_path="")
    g.messages = [{"role": "system", "content": "SYS"}]
    return g


def _steering_msgs(g):
    return [m for m in g.messages if str(m.get("content", "")).startswith(gx10._STEERING_MARKER)]


def test_run_injects_single_steering_message(monkeypatch, tmp_path):
    monkeypatch.setattr(gx10, "RAG_ENABLED", False)
    _bind(monkeypatch)
    g = _mk_agent(monkeypatch, tmp_path)
    g._generate = lambda think: ("ok", [], False, None, None)   # end the loop in one iteration
    g.run("do the thing")
    steering = _steering_msgs(g)
    assert len(steering) == 1                                   # exactly one steering message
    assert "active project: dev1test" in steering[0]["content"]
    last_user = [m for m in g.messages if m.get("role") == "user"][-1]
    assert last_user["content"] == "do the thing"              # the user turn itself stays verbatim


def test_run_keeps_single_copy_after_state_change(monkeypatch, tmp_path):
    # Codex finding #1: an authoritative block must not accumulate stale copies across a project/unit switch.
    monkeypatch.setattr(gx10, "RAG_ENABLED", False)
    g = _mk_agent(monkeypatch, tmp_path)
    g._generate = lambda think: ("ok", [], False, None, None)
    _bind(monkeypatch, project="alpha", unit="unit-a")
    g.run("turn one")
    _bind(monkeypatch, project="beta", unit="unit-b")          # state switches between turns
    g.run("turn two")
    steering = _steering_msgs(g)
    assert len(steering) == 1                                  # no stale accumulation
    assert "active project: beta" in steering[0]["content"]    # reflects the CURRENT state only
    assert "alpha" not in steering[0]["content"]


def test_run_byte_identical_when_unbound(monkeypatch, tmp_path):
    monkeypatch.setattr(gx10, "RAG_ENABLED", False)
    _bind(monkeypatch, project=None, status="unisolated", unit=None)
    g = _mk_agent(monkeypatch, tmp_path)
    g._generate = lambda think: ("ok", [], False, None, None)
    g.run("hello world")
    last_user = [m for m in g.messages if m.get("role") == "user"][-1]
    assert last_user["content"] == "hello world"                # nothing bound → verbatim
