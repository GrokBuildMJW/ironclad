"""S1: framing notes do not gate design, planning, or implementation."""
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


def _setup(monkeypatch, tmp_path, *, framing_gate=True, design_gate=False):
    gx10._apply_config(gx10._code_defaults())
    monkeypatch.setattr(gx10, "CONSTRAINT_GATE_ENABLED", framing_gate)
    monkeypatch.setattr(gx10, "DESIGN_GATE_ENABLED", design_gate)
    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", False)
    gx10.STORE = None
    monkeypatch.setattr(gx10, "_ui_print", lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)
    gx10.initiative_new("Demo", "software")


def _impl_json(title="build it", **typed):
    payload = {"type": "implementation", "priority": "high", "title": title, "description": "x"}
    payload.update(typed)
    return json.dumps(payload)


def test_implementation_handover_not_blocked_without_framing(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, framing_gate=True)

    out = gx10._stage_handover(None, "OPUS", "handover body", _impl_json())

    assert out.startswith("OK")
    assert "constraints not on record" not in out
    assert len(gx10._store().list("pending")) == 1


def test_plan_units_not_blocked_without_framing(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, framing_gate=True)
    epic = {"type": "epic", "priority": "high", "title": "Epic", "description": "e"}
    units = [{"type": "implementation", "priority": "high", "title": "U1", "description": "u"}]

    out = gx10._plan_units(json.dumps(epic), json.dumps(units))

    assert not out.startswith("ERROR")
    assert len(gx10._store().list()) == 2


def test_record_design_not_blocked_without_framing(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, framing_gate=True)

    rel = gx10.record_design("Approach", "use Python", language="python")

    assert rel.endswith("decisions/design.md")


def test_optional_framing_notes_injected_when_capture_tool_enabled(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, framing_gate=True)
    gx10.record_constraints("Framing", "prefer local")

    out = gx10._stage_handover(None, "OPUS", "handover body", _impl_json())

    assert out.startswith("OK")
    tid = gx10._store().list("pending")[0]["id"]
    md = (gx10.handovers_dir() / f"{tid}_OPUS.md").read_text(encoding="utf-8")
    assert "## Framing notes (context only" in md
    assert "prefer local" in md


def test_design_gate_still_blocks_implementation(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, framing_gate=False, design_gate=True)

    out = gx10._stage_handover(None, "OPUS", "handover body", _impl_json())

    assert out.startswith("ERROR")
    assert "design" in out.lower()
    assert gx10._store().list("pending") == []
