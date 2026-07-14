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


def _setup(monkeypatch, tmp_path, *, framing_notes=False):
    gx10._apply_config(gx10._code_defaults())
    monkeypatch.setattr(gx10, "FRAMING_NOTES_ENABLED", framing_notes)
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
    _setup(monkeypatch, tmp_path, framing_notes=False)
    assert "record_constraints" not in _tool_names()
    assert gx10.run_tool("record_constraints", {"title": "Scope", "body": "x"}) == (
        "ERROR: framing notes disabled"
    )


def test_tool_dispatch_records_framing_when_enabled(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, framing_notes=True)
    out = gx10.run_tool("record_constraints", {"title": "Scope", "body": "stay local"})
    assert out.startswith("OK: framing notes recorded at ")
    assert out.endswith("(CAPTURED).")
    assert _framing_doc().is_file()


@pytest.mark.parametrize(
    ("value", "expected"),
    [(True, True), (False, False)],
)
def test_framing_notes_config_only_controls_tool_exposure(value, expected):
    gx10._apply_framing_notes({"framing_notes": {"enabled": value}})
    assert gx10.FRAMING_NOTES_ENABLED is expected


def test_legacy_framing_alias_maps_warns_and_is_removed(capsys):
    cfg = gx10._code_defaults()
    cfg["constraint_gate"] = {"enabled": True}
    gx10._apply_config(cfg)
    assert "constraint_gate.enabled" in capsys.readouterr().out
    assert "constraint_gate" not in cfg
    assert cfg["framing_notes"]["enabled"] is True
    assert gx10.FRAMING_NOTES_ENABLED is True


def test_runtime_legacy_framing_alias_sets_canonical_key(monkeypatch):
    cfg = gx10._code_defaults()
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", cfg)
    surfaced = []
    monkeypatch.setattr(gx10, "_ui_print", lambda message, *a, **k: surfaced.append(str(message)))
    gx10._dispatch(None, "config set constraint_gate.enabled true")
    assert cfg["framing_notes"]["enabled"] is False
    assert gx10._EFFECTIVE_CFG is not cfg
    assert gx10._EFFECTIVE_CFG["framing_notes"]["enabled"] is True
    assert "constraint_gate" not in cfg
    assert gx10.FRAMING_NOTES_ENABLED is True
    assert len(surfaced) == 1 and "deprecated" in surfaced[0] and "framing_notes.enabled" in surfaced[0]


@pytest.mark.parametrize("value", [True, False, "anything"])
def test_retired_conflict_detect_warns_is_ignored_and_runtime_set_is_refused(monkeypatch, capsys, value):
    cfg = gx10._code_defaults()
    cfg["safety"]["constraint_conflict_detect"] = value
    gx10._apply_config(cfg)
    assert "safety.constraint_conflict_detect" in capsys.readouterr().out
    assert "constraint_conflict_detect" not in cfg.get("safety", {})
    assert not hasattr(gx10, "CONSTRAINT_CONFLICT_DETECT")

    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", cfg)
    surfaced = []
    monkeypatch.setattr(gx10, "_ui_print", lambda message, *a, **k: surfaced.append(str(message)))
    gx10._dispatch(None, "config set safety.constraint_conflict_detect true")
    assert len(surfaced) == 1 and "retired and cannot be set" in surfaced[0]
