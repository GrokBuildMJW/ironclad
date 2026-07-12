"""S1 framing-note capture after product constraint retirement."""
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


def _setup(monkeypatch, tmp_path, *, gate=False):
    gx10._apply_config(gx10._code_defaults())
    monkeypatch.setattr(gx10, "DESIGN_GATE_ENABLED", False)
    monkeypatch.setattr(gx10, "CONSTRAINT_GATE_ENABLED", gate)
    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", False)
    gx10.STORE = None
    monkeypatch.setattr(gx10, "_ui_print", lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)
    gx10.initiative_new("Demo", "software")


def _framing_doc() -> Path:
    return gx10.vault_root() / gx10.active_slug() / "notes" / "framing.md"


def _legacy_constraints_doc() -> Path:
    return gx10.vault_root() / gx10.active_slug() / "decisions" / "constraints.md"


def _tool_names() -> set[str]:
    return {tool["function"]["name"] for tool in gx10._effective_tools()}


def test_record_constraints_writes_framing_notes_not_decisions(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)

    rel = gx10.record_constraints("Framing", "\nkeep local\n", language="py")

    assert rel.endswith("notes/framing.md")
    assert _framing_doc().is_file()
    assert not _legacy_constraints_doc().exists()
    text = _framing_doc().read_text(encoding="utf-8")
    assert "type: note\n" in text
    assert "stage: framing\n" in text
    assert "language: python\n" in text
    assert text.endswith("keep local")
    assert gx10._constraint_status(gx10.active_slug()) == (gx10.CAPTURED, "keep local")


@pytest.mark.parametrize("body", ["", " \n", "none", " NoNe "])
def test_record_constraints_none_is_framing_none(monkeypatch, tmp_path, body):
    _setup(monkeypatch, tmp_path)

    gx10.record_constraints("None", body)

    assert "declared_none: true\n" in _framing_doc().read_text(encoding="utf-8")
    assert gx10._constraint_status(gx10.active_slug()) == (gx10.CAPTURED_NONE, None)


def test_record_constraints_reserved_marker_refuses(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)

    with pytest.raises(gx10.GateRefusal):
        gx10.record_constraints("Bad", "<!-- IRONCLAD:CONSTRAINTS -->")

    assert not _framing_doc().exists()


def test_tool_exposure_still_default_off(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, gate=False)

    assert "record_constraints" not in _tool_names()
    assert gx10.run_tool("record_constraints", {"title": "Scope", "body": "x"}) == (
        "ERROR: constraint gate disabled"
    )


def test_tool_dispatch_records_framing_when_enabled(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, gate=True)

    out = gx10.run_tool("record_constraints", {"title": "Scope", "body": "stay local"})

    assert out.startswith("OK: framing notes recorded at ")
    assert out.endswith("(CAPTURED).")
    assert _framing_doc().is_file()


@pytest.mark.parametrize(
    ("value", "expected"),
    [(True, True), (False, False), ("true", True), ("false", False), ("garbage", False)],
)
def test_constraint_gate_config_only_controls_tool_exposure(monkeypatch, value, expected):
    gx10._apply_constraint_gate({"constraint_gate": {"enabled": value}})
    assert gx10.CONSTRAINT_GATE_ENABLED is expected


def test_retired_conflict_detect_config_is_noop(monkeypatch):
    gx10._apply_constraint_conflict_detect({"safety": {"constraint_conflict_detect": True}})
    assert gx10.CONSTRAINT_CONFLICT_DETECT is False
