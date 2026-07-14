"""#1268/#1296 — enabling autonomous mode must never be a SILENT no-op. `_empty_pipeline_hint` tells the
operator what the loop will do next: name the selected next open unit, surface a selection deadlock, or —
with nothing at all — say how to seed the pipeline (plan_units). It returns None once work is in flight.
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=lambda **kw: object()))
_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402


def _setup(monkeypatch, tmp_path):
    gx10._apply_config(gx10._code_defaults())
    gx10.STORE = None
    monkeypatch.setattr(gx10, "_ui_print", lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)
    gx10.initiative_new("Demo", "software")


def test_hint_present_on_empty_pipeline(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    hint = gx10._empty_pipeline_hint()
    assert hint and "pipeline empty" in hint
    assert "plan_units" in hint                    # #1296: the seed path is the epic decomposition
    # a plain project configures no capability backlog → the no-continuation note is present
    assert "capability backlog" in hint


def test_hint_none_when_a_task_is_pending(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    # a non-implementation handover creates a pending task WITH a handover → work in flight
    gx10._stage_handover(None, "OPUS", "body",
                         json.dumps({"type": "architecture", "priority": "high",
                                     "title": "design the service architecture",
                                     "description": "Design the service architecture with clear module "
                                                    "boundaries, interfaces, and persistence."}))
    assert gx10._store().list("pending")            # a task IS pending
    assert gx10._empty_pipeline_hint() is None      # → no hint (the loop has work to do)


def test_hint_names_next_open_unit(monkeypatch, tmp_path):
    # #1296: a handover-less pending unit is NOT "work in flight" — the hint names the selected unit.
    _setup(monkeypatch, tmp_path)
    tid = gx10._store().create({"type": "documentation", "priority": "high",
                                "title": "write the readme", "description": "x"}, force=True)["id"]
    hint = gx10._empty_pipeline_hint()
    assert hint and tid in hint
    assert "stage_handover" in hint                 # the guided next step is spelled out


def test_hint_surfaces_selection_deadlock(monkeypatch, tmp_path):
    # #1296: open units that are all dependency-gated must be a VISIBLE warning, not a silent idle.
    _setup(monkeypatch, tmp_path)
    gx10._store().create({"type": "documentation", "priority": "high", "title": "gated unit",
                          "description": "x", "dependencies": ["KGC-999"]}, force=True)
    hint = gx10._empty_pipeline_hint()
    assert hint and "NONE is selectable" in hint
