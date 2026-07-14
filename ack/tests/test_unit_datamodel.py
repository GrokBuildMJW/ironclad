"""#1223 (S1) — the GitHub-issue-shaped unit data model: `labels` + `parent` on a unit.

Additive fields so a public DEV-1 unit maps 1:1 onto a (sub-)issue (the epic/sub-issue link + the label set).
Optional (empty defaults → byte-identical when unused). Tests: the ACK schema accepts them, the TaskStore
round-trips them, stage_handover carries them, and a unit without them is unaffected.
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

import pytest  # noqa: E402

import gx10  # noqa: E402
from ack.case_spec import TaskSpec  # noqa: E402


def _setup(monkeypatch, tmp_path):
    gx10._apply_config(gx10._code_defaults())
    gx10.STORE = None
    monkeypatch.setattr(gx10, "_ui_print", lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)
    gx10.initiative_new("Demo", "software")


def test_taskspec_accepts_labels_and_parent():
    t = TaskSpec(type="feature", priority="high", title="x", description="y",
                 labels=["area/engine", "type/task"], parent="KGC-3")
    assert t.labels == ["area/engine", "type/task"] and t.parent == "KGC-3"


def test_taskspec_defaults_are_empty():
    t = TaskSpec(type="feature", priority="high", title="x", description="y")
    assert t.labels == [] and t.parent is None


def test_invalid_parent_rejected():
    # the parent/epic link must be a real unit-id (like dependencies) — garbage is rejected at emission.
    with pytest.raises(Exception):
        TaskSpec(type="feature", priority="high", title="x", description="y", parent="not a parent")


def test_store_roundtrips_labels_and_parent(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    tid = gx10._store().create(
        {"type": "feature", "priority": "high", "title": "x", "description": "y",
         "labels": ["area/engine"], "parent": "KGC-2"}, force=True)["id"]
    got = gx10._store().get(tid)
    assert got["labels"] == ["area/engine"] and got["parent"] == "KGC-2"


def test_unit_without_fields_is_unaffected(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    tid = gx10._store().create(
        {"type": "feature", "priority": "high", "title": "x", "description": "y"}, force=True)["id"]
    got = gx10._store().get(tid)
    assert not got.get("labels")            # empty/absent → byte-identical
    assert not got.get("parent")


def test_stage_handover_carries_labels_and_parent(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    tj = json.dumps({"type": "documentation", "priority": "high", "title": "Document the unit model",
                     "description": "Document the complete unit data model and preserve its metadata fields.",
                     "labels": ["area/ci"], "parent": "KGC-1"})
    gx10._stage_handover(None, "OPUS", "handover body", tj)
    got = gx10._store().get(gx10._store().list("pending")[0]["id"])
    assert got["labels"] == ["area/ci"] and got["parent"] == "KGC-1"
