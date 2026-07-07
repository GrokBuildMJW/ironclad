"""#1227 (S5) — the fail-closed design→impl approval gate (no blind coding, R2/R3).

An IMPLEMENTATION stage_handover is REFUSED until the active unit has a recorded + APPROVED design; design/
analysis handovers pass through. `record_design` persists the design (approved:false); `/approve` stamps it.
These tests drive the real `_stage_handover` path + the record_design→approve round-trip.
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


def _setup(monkeypatch, tmp_path, *, gate=True):
    gx10._apply_config(gx10._code_defaults())
    monkeypatch.setattr(gx10, "DESIGN_GATE_ENABLED", gate)   # the gate is opt-in (default OFF) — enable it here
    gx10.STORE = None
    monkeypatch.setattr(gx10, "_ui_print", lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)
    gx10.initiative_new("Demo", "software")


def test_gate_off_by_default_allows_impl(monkeypatch, tmp_path):
    # opt-in: with the gate DISABLED (the default), an implementation handover is byte-identical (allowed).
    _setup(monkeypatch, tmp_path, gate=False)
    out = _stage(_impl_json())                               # no design, but the gate is off
    assert "refused" not in out.lower()
    assert len(_pending()) == 1


def _impl_json(title="build it"):
    return json.dumps({"type": "implementation", "priority": "high", "title": title, "description": "x"})


def _stage(task_json):
    return gx10._stage_handover(None, "OPUS", "handover body", task_json)


def _pending():
    return gx10._store().list("pending")


def test_impl_refused_without_design(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    out = _stage(_impl_json())
    assert "blind-coding refused" in out
    assert _pending() == []                                  # fail-closed BEFORE store.create — nothing created


def test_impl_refused_with_unapproved_design(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    gx10.record_design("Approach", "use Rust")
    out = _stage(_impl_json())
    assert "NOT approved" in out
    assert _pending() == []


def test_impl_allowed_with_approved_design(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    gx10.record_design("Approach", "use Rust")
    gx10._approve_design()
    out = _stage(_impl_json())
    assert "refused" not in out.lower() and "NOT approved" not in out
    assert len(_pending()) == 1                              # allowed → task created


def test_non_impl_unaffected(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    tj = json.dumps({"type": "architecture", "priority": "high", "title": "design it", "description": "x"})
    out = _stage(tj)
    assert "refused" not in out.lower()
    assert len(_pending()) == 1                              # design/analysis handover is NOT gated


def test_pure_rehandover_unaffected(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    gx10.record_design("Approach", "use Rust")
    gx10._approve_design()
    _stage(_impl_json())
    tid = _pending()[0]["id"]
    out = gx10._stage_handover(tid, "OPUS", "re-handover", None)   # task_json=None → not gated
    assert "refused" not in out.lower()


def test_force_does_not_bypass_gate(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    out = gx10._stage_handover(None, "OPUS", "body", _impl_json(), True, True)  # force=True
    assert "blind-coding refused" in out
    assert _pending() == []


def test_record_design_approve_roundtrip(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    slug = gx10.active_slug()
    assert gx10._unit_design_status(slug) == (False, False, None)
    rel = gx10.record_design("Approach", "use Rust")
    assert rel.endswith("decisions/design.md")               # single canonical design doc
    hd, ap, ref = gx10._unit_design_status(slug)
    assert hd and not ap
    msg = gx10._approve_design()
    assert msg.startswith("OK")
    hd, ap, _ = gx10._unit_design_status(slug)
    assert hd and ap


def test_approve_without_design(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    out = gx10._approve_design()
    assert out.startswith("ERROR") and "no design" in out.lower()


def test_record_design_resets_approval(monkeypatch, tmp_path):
    # Sonnet finding #1: a newly-recorded design (even under a DIFFERENT title) must NOT be waved through by a
    # stale approval — one canonical design doc, re-recording resets it, so the gate re-closes.
    _setup(monkeypatch, tmp_path)
    gx10.record_design("Approach", "use Rust")
    gx10._approve_design()
    assert gx10._unit_design_status(gx10.active_slug())[1] is True
    gx10.record_design("Auth redesign", "use Go instead")     # a new/changed design must be re-approved
    assert gx10._unit_design_status(gx10.active_slug())[1] is False
    assert gx10._design_gate("implementation", gx10.active_slug()).startswith("ERROR")  # gate re-closed


def test_rehandover_of_impl_task_gated_when_unapproved(monkeypatch, tmp_path):
    # Sonnet finding #3: re-handing an impl task (task_json=None) cannot bypass the gate.
    _setup(monkeypatch, tmp_path)
    gx10.record_design("Approach", "use Rust")
    gx10._approve_design()
    _stage(_impl_json())                                      # impl task created (design approved)
    tid = _pending()[0]["id"]
    gx10.record_design("Auth redesign", "changed")            # un-approves the design
    out = gx10._stage_handover(tid, "OPUS", "impl now", None)  # re-hand the impl task with no task_json
    assert "NOT approved" in out                              # refused — no bypass


def test_rehandover_unknown_task(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    out = gx10._stage_handover("KGC-999", "OPUS", "body", None)
    assert out.startswith("ERROR: no such task")


def test_design_gate_unit(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    slug = gx10.active_slug()
    assert gx10._design_gate("documentation", slug) is None                  # non-impl ungated
    assert gx10._design_gate("implementation", slug).startswith("ERROR")     # no design
    assert gx10._design_gate("implementation", None).startswith("ERROR")     # no unit
