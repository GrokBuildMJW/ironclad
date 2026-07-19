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
    monkeypatch.setattr(gx10, "FRAMING_NOTES_ENABLED", True)
    gx10.STORE = None
    monkeypatch.setattr(gx10, "_ui_print", lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)
    gx10.initiative_new("Demo", "software")


def _impl_json(title="build it", **typed):
    payload = {"type": "implementation", "priority": "high", "title": f"Implement approved {title}",
               "description": "Implement the approved design with complete validation and regression coverage."}
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

    # #1613: an ad-hoc implementation task_json is now refused at the model tool-dispatch (run_tool). The
    # core-API `_stage_handover` still enforces the approved design's typed language for such a create, so the
    # design-standard behaviour is exercised directly here (the dispatch-level ad-hoc refusal + steer-to-
    # plan_units is covered in test_plan_units).
    drift = gx10._stage_handover(
        None, "OPUS", "body", task_json=_impl_json("rust", language="rust"),
    )
    assert drift.startswith("ERROR")
    assert "approved design requires language='python'" in drift

    ok = gx10._stage_handover(
        None, "OPUS", "body", task_json=_impl_json("python", language="python"),
    )
    assert ok.startswith("OK")
    tid = gx10._store().list("pending")[0]["id"]
    md = next(gx10.handovers_dir().glob(f"{tid}_*.md")).read_text(encoding="utf-8")
    assert "## Approved design standard" in md
    assert "- language: python" in md
    assert "- stdlib only" in md


def test_framing_off_still_requires_and_injects_approved_design(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(gx10, "FRAMING_NOTES_ENABLED", False)

    assert gx10.run_tool("record_constraints", {"title": "Context", "body": "x"}) == (
        "ERROR: framing notes disabled"
    )
    # #1613: the core-API `_stage_handover` enforces the blind-coding design gate for an ad-hoc create; the
    # model tool-dispatch refuses ad-hoc implementation outright (see test_plan_units).
    refused = gx10._stage_handover(
        None, "OPUS", "body", task_json=_impl_json("before design"),
    )
    assert "blind-coding refused" in refused
    assert gx10._store().list("pending") == []

    gx10.record_design("Approach", "Use Python.", language="python")
    assert gx10._approve_design().startswith("OK")
    out = gx10._stage_handover(
        None, "OPUS", "body", task_json=_impl_json(language="python"),
    )

    assert out.startswith("OK")
    tid = gx10._store().list("pending")[0]["id"]
    md = next(gx10.handovers_dir().glob(f"{tid}_*.md")).read_text(encoding="utf-8")
    assert "## Approved design standard" in md
    assert "- language: python" in md
