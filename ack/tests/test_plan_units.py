"""plan_units + epic lifecycle (#1296) — the design decomposition macro and its loop mechanics.

Covers: the batch macro (one epic + N parent-linked, handover-less pending units; atomic +
fail-closed; in-batch and store dedup; `unit:<n>` sibling-dependency resolution; `epic_id`
append; done-epic refusal; design gate), the engine-side epic auto-complete on the last child's
advance, the re-hand staging parity (id normalization + `assigned_to` stamp), and the `/auto`
automation meta-switch. Integration-grade: real TaskStore under a temp project.
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=lambda **kw: object()))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402

_EPIC = {"priority": "high", "title": "file-search CLI", "description": "the approved design"}


def _u(n: int, *, typ: str = "implementation", prio: str = "high", **kw) -> dict:
    return {"type": typ, "priority": prio, "title": f"unit number {n} does thing {n}",
            "description": f"build part {n} of the design", **kw}


@pytest.fixture(autouse=True)
def _project(tmp_path, monkeypatch):
    gx10._apply_config(gx10._code_defaults())
    gx10.STORE = None
    monkeypatch.setattr(gx10, "_ui_print", lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)
    gx10.initiative_new("Demo", "software")
    gx10.record_design("Approved approach", "Use the approved approach.")
    assert gx10._approve_design().startswith("OK")
    return tmp_path


# ── the batch macro ──────────────────────────────────────────────────────────

def test_plan_units_creates_epic_and_parent_linked_units(tmp_path):
    out = gx10._plan_units(json.dumps(_EPIC),
                           json.dumps([_u(1), _u(2, dependencies=["unit:1"]), _u(3, dependencies=["unit:2"])]))
    assert out.startswith("OK:"), out
    store = gx10._store()
    epics = [t for t in store.list("pending") if t.get("type") == "epic"]
    units = [t for t in store.list("pending") if t.get("type") != "epic"]
    assert len(epics) == 1 and len(units) == 3
    eid = epics[0]["id"]
    assert all(t.get("parent") == eid for t in units)
    # deliberately handover-less: the handover is authored at select time
    assert all(gx10._find_handover(t["id"]) is None for t in units)
    assert "Next open unit" in out                      # the guided next step is named
    board = (tmp_path / "vault" / "demo" / "BOARD.md").read_text(encoding="utf-8")
    assert "units: 0/3 done" in board                   # per-epic progress on the board


def test_plan_units_is_atomic_on_an_invalid_unit():
    bad = _u(2); bad["type"] = "not-a-type"
    out = gx10._plan_units(json.dumps(_EPIC), json.dumps([_u(1), bad]))
    assert out.startswith("ERROR") and "unit 2" in out
    assert gx10._store().list() == []                   # NOTHING created (epic included)


def test_plan_units_refuses_in_batch_duplicates():
    out = gx10._plan_units(json.dumps(_EPIC), json.dumps([_u(1), _u(1)]))
    assert out.startswith("ERROR") and "duplicates of each other" in out
    assert gx10._store().list() == []


def test_plan_units_dedups_against_done_tasks():
    store = gx10._store()
    tid = store.create(dict(_u(1)), force=True)["id"]
    store.transition(tid, "done")
    out = gx10._plan_units(json.dumps(_EPIC), json.dumps([_u(1)]))
    assert out.startswith("ERROR") and tid in out       # done tasks stay in the dedup horizon


def test_plan_units_resolves_sibling_placeholder_deps():
    out = gx10._plan_units(json.dumps(_EPIC),
                           json.dumps([_u(1), _u(2, dependencies=["unit:1"])]))
    assert out.startswith("OK:")
    units = sorted((t for t in gx10._store().list("pending") if t.get("type") != "epic"),
                   key=lambda t: t["id"])
    assert units[1]["dependencies"] == [units[0]["id"]]


def test_plan_units_refuses_bad_placeholder():
    out = gx10._plan_units(json.dumps(_EPIC), json.dumps([_u(1, dependencies=["unit:1"])]))
    assert out.startswith("ERROR") and "not another unit" in out
    assert gx10._store().list() == []


def test_plan_units_epic_id_appends_and_done_epic_refuses():
    gx10._plan_units(json.dumps(_EPIC), json.dumps([_u(1)]))
    store = gx10._store()
    eid = next(t["id"] for t in store.list("pending") if t.get("type") == "epic")
    out = gx10._plan_units(None, json.dumps([_u(2)]), epic_id=eid)
    assert out.startswith("OK:")
    assert sum(1 for t in store.list("pending")
               if t.get("parent") == eid) == 2
    store.transition(eid, "done")
    out = gx10._plan_units(None, json.dumps([_u(3)]), epic_id=eid)
    assert out.startswith("ERROR") and "already done" in out


def test_plan_units_rejects_nested_epics():
    out = gx10._plan_units(json.dumps(_EPIC), json.dumps([_u(1, typ="epic")]))
    assert out.startswith("ERROR") and "one level" in out


def test_plan_units_design_gate_blocks_impl_units(monkeypatch):
    (gx10.vault_root() / gx10.active_slug() / "decisions" / "design.md").unlink()
    for proposal in (gx10.vault_root() / gx10.active_slug() / "proposals").glob("design-*.md"):
        proposal.unlink()
    out = gx10._plan_units(json.dumps(_EPIC), json.dumps([_u(1)]))
    assert out.startswith("ERROR") and "blind-coding refused" in out
    assert gx10._store().list() == []


def test_plan_units_refuses_language_drift_before_creating_anything(monkeypatch):
    gx10.record_design("Approach", "Use Python.", language="python")
    assert gx10._approve_design(design_id="2").startswith("OK")

    out = gx10._plan_units(json.dumps(_EPIC), json.dumps([_u(1, language="rust")]))

    assert out.startswith("ERROR")
    assert "approved design requires language='python'" in out
    assert "task provides 'rust'" in out
    assert gx10._store().list() == []


# ── epic auto-complete on the last child's advance ───────────────────────────

def _advance(tid: str) -> str:
    fb = gx10.feedback_dir() / f"{tid}_OPUS-feedback.md"
    fb.parent.mkdir(parents=True, exist_ok=True)
    fb.write_text("---\nstatus: done\n---\n\n## Result\nok", encoding="utf-8")
    return gx10._advance_pipeline(tid, "OPUS")


def test_epic_auto_completes_when_last_unit_advances():
    gx10._plan_units(json.dumps(_EPIC), json.dumps([_u(1), _u(2)]))
    store = gx10._store()
    eid = next(t["id"] for t in store.list("pending") if t.get("type") == "epic")
    u1, u2 = sorted(t["id"] for t in store.list("pending") if t.get("type") != "epic")

    gx10._stage_handover(u1, "OPUS", f"---\nto: OPUS\ntask_id: {u1}\n---\nbuild part 1")
    out = _advance(u1)
    assert "1 unit(s) still open" in out                # epic held open, visibly
    assert store.get(eid)["status"] == "pending"

    gx10._stage_handover(u2, "OPUS", f"---\nto: OPUS\ntask_id: {u2}\n---\nbuild part 2")
    out = _advance(u2)
    assert f"epic {eid} auto-completed (all 2 units done)" in out
    assert store.get(eid)["status"] == "done"           # derived completion — no feedback needed


def test_advance_without_parent_is_unchanged():
    store = gx10._store()
    tid = store.create(dict(_u(1)), force=True)["id"]
    gx10._stage_handover(tid, "OPUS", "body")
    out = _advance(tid)
    assert "ERROR" not in out and "epic" not in out     # no parent → no epic bookkeeping


# ── re-hand staging parity (#1296) ───────────────────────────────────────────

def test_rehand_normalizes_id_and_stamps_assigned_to():
    store = gx10._store()
    tid = store.create(dict(_u(1)), force=True)["id"]
    out = gx10._stage_handover(tid, "OPUS",
                               "---\nto: OPUS\ntask_id: KGC-999\n---\nbody with a stale id")
    assert "ERROR" not in out
    ho = gx10.handovers_dir() / f"{tid}_OPUS.md"
    assert f"task_id: {tid}" in ho.read_text(encoding="utf-8")   # normalized, not KGC-999
    assert store.get(tid).get("assigned_to") == "OPUS"           # canonical identity stamped


# ── /auto — the automation meta-switch ───────────────────────────────────────

def test_watcher_defaults_off_and_config_cannot_enable_it_independently(monkeypatch):
    saved = (gx10._WATCHER_ENABLED, gx10.AUTOPILOT_ENABLED, gx10.AUTOPILOT_AUTOPLAN,
             gx10._EFFECTIVE_CFG)
    try:
        cfg = gx10._code_defaults()
        assert "enabled" not in cfg["watcher"]
        gx10._apply_config(cfg)
        assert gx10._WATCHER_ENABLED is False

        surfaced = []
        gx10._EFFECTIVE_CFG = cfg
        monkeypatch.setattr(gx10, "_ui_print", lambda message, *a, **k: surfaced.append(str(message)))
        gx10._dispatch(None, "config set watcher.enabled true")

        assert gx10._WATCHER_ENABLED is False
        assert "enabled" not in cfg["watcher"]
        assert len(surfaced) == 1 and "retired and cannot be set" in surfaced[0]
    finally:
        (gx10._WATCHER_ENABLED, gx10.AUTOPILOT_ENABLED, gx10.AUTOPILOT_AUTOPLAN,
         gx10._EFFECTIVE_CFG) = saved


def test_auto_on_off_flips_the_three_flags():
    saved = (gx10._WATCHER_ENABLED, gx10.AUTOPILOT_ENABLED, gx10.AUTOPILOT_AUTOPLAN,
             gx10.AUTOPILOT_MAX_TASKS, gx10._AUTOPLAN_DONE, gx10._EFFECTIVE_CFG)
    try:
        gx10._EFFECTIVE_CFG = {"watcher": {}, "autopilot": {}, "paths": {}}
        gx10._dispatch(None, "auto on 5")
        assert gx10._WATCHER_ENABLED and gx10.AUTOPILOT_ENABLED and gx10.AUTOPILOT_AUTOPLAN
        assert gx10.AUTOPILOT_MAX_TASKS == 5 and gx10._AUTOPLAN_DONE == 0
        assert gx10._EFFECTIVE_CFG["autopilot"] == {"enabled": True, "autoplan": True,
                                                    "autoplan_max_tasks": 5}
        gx10._dispatch(None, "auto off")
        assert not (gx10._WATCHER_ENABLED or gx10.AUTOPILOT_ENABLED or gx10.AUTOPILOT_AUTOPLAN)
    finally:
        (gx10._WATCHER_ENABLED, gx10.AUTOPILOT_ENABLED, gx10.AUTOPILOT_AUTOPLAN,
         gx10.AUTOPILOT_MAX_TASKS, gx10._AUTOPLAN_DONE, gx10._EFFECTIVE_CFG) = saved


def test_watcher_command_delegates_to_auto_meta_switch(monkeypatch):
    saved = (gx10._WATCHER_ENABLED, gx10.AUTOPILOT_ENABLED, gx10.AUTOPILOT_AUTOPLAN,
             gx10.AUTOPILOT_MAX_TASKS, gx10._AUTOPLAN_DONE, gx10._EFFECTIVE_CFG)
    try:
        monkeypatch.setattr(gx10, "_ui_print", lambda *a, **k: None)
        gx10._EFFECTIVE_CFG = {"watcher": {}, "autopilot": {}, "paths": {}}
        gx10._WATCHER_ENABLED = False
        gx10.AUTOPILOT_ENABLED = False
        gx10.AUTOPILOT_AUTOPLAN = False

        gx10._dispatch(None, "watcher on")
        assert gx10._WATCHER_ENABLED and gx10.AUTOPILOT_ENABLED and gx10.AUTOPILOT_AUTOPLAN
        assert "enabled" not in gx10._EFFECTIVE_CFG["watcher"]
        assert gx10._EFFECTIVE_CFG["autopilot"]["enabled"] is True

        gx10._dispatch(None, "watcher off")
        assert not (gx10._WATCHER_ENABLED or gx10.AUTOPILOT_ENABLED or gx10.AUTOPILOT_AUTOPLAN)
        assert "enabled" not in gx10._EFFECTIVE_CFG["watcher"]
        assert gx10._EFFECTIVE_CFG["autopilot"]["enabled"] is False
    finally:
        (gx10._WATCHER_ENABLED, gx10.AUTOPILOT_ENABLED, gx10.AUTOPILOT_AUTOPLAN,
         gx10.AUTOPILOT_MAX_TASKS, gx10._AUTOPLAN_DONE, gx10._EFFECTIVE_CFG) = saved


def test_steering_state_recommends_next_unit():
    gx10._plan_units(json.dumps(_EPIC), json.dumps([_u(1)]))
    block = gx10._steering_state_block()
    assert "next open unit:" in block and "stage_handover" in block
    assert "continuation:" in block                     # the automation mode is part of the state


def test_guided_advance_result_recommends_next_unit(monkeypatch):
    # #1296 (guided mode): with continuation OFF, the advance RESULT itself names the next unit.
    monkeypatch.setattr(gx10, "AUTOPILOT_AUTOPLAN", False)
    gx10._plan_units(json.dumps(_EPIC), json.dumps([_u(1), _u(2)]))
    store = gx10._store()
    u1, u2 = sorted(t["id"] for t in store.list("pending") if t.get("type") != "epic")
    gx10._stage_handover(u1, "OPUS", "body")
    out = _advance(u1)
    assert f"Next open unit: {u2}" in out and "stage_handover" in out


def test_plan_units_result_bootstraps_when_armed(monkeypatch):
    # #1296: with the continuation armed, the plan_units result has THIS turn author the first
    # unit's handover (the bootstrap); disarmed, it recommends /auto on or the guided staging.
    monkeypatch.setattr(gx10, "AUTOPILOT_AUTOPLAN", True)
    out = gx10._plan_units(json.dumps(_EPIC), json.dumps([_u(1)]))
    assert "AUTOMATION ARMED" in out and "stage_handover" in out
    # #1309: autoplan-only (no active launcher) must still tell the model to launch — never strand it
    assert "launch it with launch_coder" in out and "do NOT call launch_coder" not in out


def test_plan_units_armed_defers_launch_when_auto_owns_launching(monkeypatch):
    # #1309: when the loop actually owns launching (autopilot + watcher = the /auto meta-switch), the armed
    # prompt tells the model NOT to call launch_coder — the loop launches, so no double-drive.
    monkeypatch.setattr(gx10, "AUTOPILOT_AUTOPLAN", True)
    monkeypatch.setattr(gx10, "AUTOPILOT_ENABLED", True, raising=False)
    monkeypatch.setattr(gx10, "_WATCHER_ENABLED", True, raising=False)
    out = gx10._plan_units(json.dumps(_EPIC), json.dumps([_u(1)]))
    assert "AUTOMATION ARMED" in out and "do NOT call launch_coder" in out


def test_plan_units_result_recommends_auto_when_disarmed(monkeypatch):
    monkeypatch.setattr(gx10, "AUTOPILOT_AUTOPLAN", False)
    out = gx10._plan_units(json.dumps(_EPIC), json.dumps([_u(1)]))
    assert "/auto on" in out and "AUTOMATION ARMED" not in out


def test_plan_units_warns_when_multi_unit_plan_declares_no_dependencies():
    # #1310: the engine honours declared dependencies topologically, but a small model often omits them —
    # a multi-unit plan with ZERO declared deps gets a steering NOTE (else the units are ordered by
    # priority, not build order → e.g. a module before its scaffolding).
    out = gx10._plan_units(json.dumps(_EPIC), json.dumps([_u(1), _u(2), _u(3)]))
    assert out.startswith("OK:")
    assert "no inter-unit dependencies were declared" in out


def test_plan_units_no_dep_warning_when_dependencies_declared():
    # #1310: a plan that DOES declare a build-order dependency (unit:<n>) is silent — no nudge.
    out = gx10._plan_units(json.dumps(_EPIC), json.dumps([_u(1), _u(2, dependencies=["unit:1"])]))
    assert out.startswith("OK:")
    assert "no inter-unit dependencies were declared" not in out


def test_plan_units_armed_zero_deps_asks_for_the_foundational_unit(monkeypatch):
    # #1310 (Codex review): under /auto (armed) with a multi-unit plan and NO declared deps, the bootstrap
    # must NOT auto-point at the priority-selected unit (it may be a module before its scaffolding) — it
    # asks the model to author the FOUNDATIONAL unit, so automation never blindly starts a mis-ordered build.
    monkeypatch.setattr(gx10, "AUTOPILOT_AUTOPLAN", True)
    out = gx10._plan_units(json.dumps(_EPIC), json.dumps([_u(1), _u(2), _u(3)]))
    assert "AUTOMATION ARMED" in out and "FOUNDATIONAL" in out
    assert "no inter-unit dependencies were declared" in out


def test_plan_units_warns_when_only_external_deps_no_sibling_order():
    # #1310 (Codex review): a unit depending on an EXISTING task id (not a sibling `unit:<n>`) does not
    # order the NEW units among themselves — the build-order note must still fire (no sibling edges).
    prior = gx10._store().create({"type": "implementation", "priority": "low",
                                  "title": "some prior thing", "description": "x"}, force=True)["id"]
    out = gx10._plan_units(json.dumps(_EPIC),
                           json.dumps([_u(1, dependencies=[prior]), _u(2, dependencies=[prior])]))
    assert out.startswith("OK:")
    assert "no inter-unit dependencies were declared" in out
