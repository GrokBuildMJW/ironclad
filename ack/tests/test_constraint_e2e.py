"""S1 end-to-end flow: framing notes plus approved-design build enforcement."""
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
    monkeypatch.setattr(gx10, "DESIGN_GATE_ENABLED", True)
    monkeypatch.setattr(gx10, "CONSTRAINT_GATE_ENABLED", True)
    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", False)
    gx10.STORE = None
    monkeypatch.setattr(gx10, "_ui_print", lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)
    gx10.initiative_new("Demo", "software")


def _impl_json(title="build it", **typed):
    payload = {"type": "implementation", "priority": "high", "title": title, "description": "x"}
    payload.update(typed)
    return json.dumps(payload)


def test_run_tool_flow_records_framing_and_enforces_approved_design(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)

    framing = gx10.run_tool("record_constraints", {"title": "Context", "body": "prefer local"})
    assert framing.startswith("OK: framing notes recorded at ")
    assert "notes/framing.md" in framing

    design = gx10.run_tool(
        "record_design",
        {"title": "Approach", "body": "Use Python.\n\n## Build policy\n\n- stdlib only", "language": "python"},
    )
    assert design.startswith("OK: design proposal recorded at ")
    assert gx10._approve_design().startswith("OK")

    drift = gx10.run_tool(
        "stage_handover",
        {"agent": "OPUS", "handover_md": "body", "task_json": _impl_json("rust", language="rust")},
    )
    assert drift.startswith("ERROR")
    assert "approved design requires language='python'" in drift

    ok = gx10.run_tool(
        "stage_handover",
        {"agent": "OPUS", "handover_md": "body", "task_json": _impl_json("python", language="python")},
    )
    assert ok.startswith("OK")
    tid = gx10._store().list("pending")[0]["id"]
    md = next(gx10.handovers_dir().glob(f"{tid}_*.md")).read_text(encoding="utf-8")
    assert "## Approved design standard" in md
    assert "- language: python" in md
    assert "- stdlib only" in md


def test_gate_off_flow_is_byte_identical_for_missing_framing(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(gx10, "CONSTRAINT_GATE_ENABLED", False)
    monkeypatch.setattr(gx10, "DESIGN_GATE_ENABLED", False)

    assert gx10.run_tool("record_constraints", {"title": "Context", "body": "x"}) == (
        "ERROR: constraint gate disabled"
    )
    out = gx10.run_tool(
        "stage_handover",
        {"agent": "OPUS", "handover_md": "body", "task_json": _impl_json()},
    )

    assert out.startswith("OK")
    tid = gx10._store().list("pending")[0]["id"]
    md = next(gx10.handovers_dir().glob(f"{tid}_*.md")).read_text(encoding="utf-8")
    assert "IRONCLAD:CONSTRAINTS" not in md
