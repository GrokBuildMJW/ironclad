"""L1 constraint capture: canonical persistence, tri-state validity, and default-off exposure."""
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


def _setup(monkeypatch, tmp_path, *, gate=False):
    gx10._apply_config(gx10._code_defaults())
    monkeypatch.setattr(gx10, "DESIGN_GATE_ENABLED", False)
    monkeypatch.setattr(gx10, "CONSTRAINT_GATE_ENABLED", gate)
    gx10.STORE = None
    monkeypatch.setattr(gx10, "_ui_print", lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)
    gx10.initiative_new("Demo", "software")


def _doc() -> Path:
    return gx10.vault_root() / gx10.active_slug() / "decisions" / "constraints.md"


def _tool_names() -> set[str]:
    return {tool["function"]["name"] for tool in gx10._effective_tools()}


def _impl_json() -> str:
    return json.dumps({"type": "implementation", "priority": "high", "title": "build it", "description": "x"})


def test_record_constraints_round_trip_preserves_body(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    body = "\r\n  Windows line\r\n\r\nsecond line  \r\n"

    rel = gx10.record_constraints("Scope floor", body)

    assert rel.startswith("vault/") and rel.endswith("decisions/constraints.md")
    assert ((gx10._project_root() or Path.cwd()) / rel).is_file()
    text = _doc().read_bytes().decode("utf-8")
    assert text.startswith(
        "---\ntype: decision\nstage: constraints\ndeclared_none: false\ntitle: Scope floor\n---\n"
    )
    expected = "  Windows line\r\n\r\nsecond line  "
    assert text.endswith(expected)
    assert gx10._constraint_status(gx10.active_slug()) == (gx10.CAPTURED, expected)


@pytest.mark.parametrize("sentinel", ["", " \t\n", "none", " NoNe "])
def test_record_constraints_captured_none_sentinels(monkeypatch, tmp_path, sentinel):
    _setup(monkeypatch, tmp_path)

    gx10.record_constraints("No constraints", sentinel)

    text = _doc().read_text(encoding="utf-8")
    assert "declared_none: true\n" in text
    assert text.endswith("---\n")
    assert gx10._constraint_status(gx10.active_slug()) == (gx10.CAPTURED_NONE, None)


def test_record_constraints_requires_a_real_unit(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(gx10, "active_slug", lambda: None)
    with pytest.raises(ValueError, match="no active unit for record_constraints"):
        gx10.record_constraints("Scope", "stay local")
    with pytest.raises(ValueError):
        gx10.record_constraints("Scope", "stay local", slug="missing-unit")


@pytest.mark.parametrize("marker", gx10._CONSTRAINT_MARKERS)
def test_record_constraints_refuses_reserved_markers(monkeypatch, tmp_path, marker):
    _setup(monkeypatch, tmp_path)

    with pytest.raises(gx10.GateRefusal) as exc:
        gx10.record_constraints("Poisoned", f"before\n{marker}\nafter")

    assert str(exc.value) == "constraints body may not contain the reserved IRONCLAD:CONSTRAINTS marker"
    assert not _doc().exists()


def test_constraint_status_missing_and_no_unit_are_uncaptured(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    assert gx10._constraint_status(None) == (gx10.UNCAPTURED, None)
    assert gx10._constraint_status(gx10.active_slug()) == (gx10.UNCAPTURED, None)


def test_constraint_status_rejects_oversized_document(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _doc().parent.mkdir(parents=True, exist_ok=True)
    _doc().write_bytes(b"---\ntype: decision\n---\n" + b"x" * 65536)
    assert gx10._constraint_status(gx10.active_slug()) == (gx10.UNCAPTURED, None)


def test_constraint_status_rejects_decode_error(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _doc().parent.mkdir(parents=True, exist_ok=True)
    _doc().write_bytes(b"---\ntype: decision\n---\n\xff")
    assert gx10._constraint_status(gx10.active_slug()) == (gx10.UNCAPTURED, None)


@pytest.mark.parametrize(
    "text",
    [
        "---\ntype: decision\ndeclared_none: false\nbody without closure",
        "---\n---\nbody",
        "---\ntype: decision\ndeclared_none: true\n---\nbody",
        "---\ntype: decision\ndeclared_none: false\n---\n",
        "---\ntype: decision\ndeclared_none: false\n---\n<!-- IRONCLAD:CONSTRAINTS -->",
        "---\ntype: decision\ndeclared_none: false\n---\n<!-- /IRONCLAD:CONSTRAINTS -->",
    ],
    ids=["unterminated", "empty-frontmatter", "none-with-body", "false-without-body", "open-marker", "close-marker"],
)
def test_constraint_status_rejects_invalid_documents(monkeypatch, tmp_path, text):
    _setup(monkeypatch, tmp_path)
    _doc().parent.mkdir(parents=True, exist_ok=True)
    _doc().write_text(text, encoding="utf-8", newline="\n")
    assert gx10._constraint_status(gx10.active_slug()) == (gx10.UNCAPTURED, None)


def test_constraint_tool_exposure_and_dispatch_are_off_gated(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, gate=False)
    assert "record_constraints" not in _tool_names()
    assert gx10.run_tool("record_constraints", {"title": "Scope", "body": "stay local"}) == (
        "ERROR: constraint gate disabled"
    )
    assert not _doc().exists()


def test_constraint_tool_exposed_and_dispatched_when_enabled(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, gate=True)
    assert "record_constraints" in _tool_names()

    out = gx10.run_tool("record_constraints", {"title": "Scope", "body": "stay local"})

    assert out.startswith("OK: constraints recorded at ") and out.endswith("(CAPTURED).")
    assert gx10._constraint_status(gx10.active_slug()) == (gx10.CAPTURED, "stay local")


def test_constraint_dispatch_reports_typed_refusal_once(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, gate=True)
    out = gx10.run_tool(
        "record_constraints",
        {"title": "Poisoned", "body": "<!-- IRONCLAD:CONSTRAINTS -->"},
    )
    assert out == "ERROR: constraints body may not contain the reserved IRONCLAD:CONSTRAINTS marker"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, True),
        (False, False),
        (1, False),
        ("true", True),
        (" YES ", True),
        ("on", True),
        ("1", True),
        ("false", False),
        ("0", False),
        ("", False),
        ("garbage", False),
    ],
)
def test_constraint_config_uses_strict_boolean(monkeypatch, value, expected):
    gx10._apply_constraint_gate({"constraint_gate": {"enabled": value}})
    assert gx10.CONSTRAINT_GATE_ENABLED is expected


def test_constraint_config_fails_soft(monkeypatch):
    monkeypatch.setattr(gx10, "_cfg_get", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    gx10._apply_constraint_gate({})
    assert gx10.CONSTRAINT_GATE_ENABLED is False


@pytest.mark.parametrize(
    ("fragment", "expected"),
    [
        ({"constraint_gate": {"enabled": True}}, True),
        ({"constraint_gate": {"enabled": False}}, False),
        ({}, False),  # missing key → public default off
        ({"constraint_gate": {"enabled": "false"}}, False),  # strict _as_bool rejects stringy false
        ({"constraint_gate": {"enabled": "garbage"}}, False),  # malformed → False
    ],
    ids=["enabled-true", "enabled-false", "missing", "string-false", "garbage"],
)
def test_apply_config_constraint_gate_synthetic(fragment, expected):
    """#1345: boundary-clean public proof — synthetic dict only (never conf/local.json)."""
    cfg = gx10._code_defaults()
    cfg.update(fragment)
    gx10._apply_config(cfg)
    assert gx10.CONSTRAINT_GATE_ENABLED is expected


@pytest.mark.parametrize(
    ("body", "expected_line"),
    [
        (None, "- constraints: NOT on record — design/decomposition/implementation handovers BLOCKED "
               "(call record_constraints)"),
        ("none", "- constraints: none on record — OK"),
        ("stay local", "- constraints: on record — OK"),
    ],
)
def test_constraint_status_is_surfaced_only_when_enabled(monkeypatch, tmp_path, body, expected_line):
    _setup(monkeypatch, tmp_path, gate=True)
    if body is not None:
        gx10.record_constraints("Scope", body)
    assert expected_line in gx10._steering_state_block()


def test_constraint_gate_off_leaves_steering_and_handover_paths_untouched(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, gate=False)
    monkeypatch.setattr(
        gx10,
        "_constraint_status",
        lambda slug: (_ for _ in ()).throw(AssertionError("off path must not read constraints")),
    )

    block = gx10._steering_state_block()
    out = gx10._stage_handover(None, "OPUS", "handover body", _impl_json())

    assert "constraints:" not in block
    assert "refused" not in out.lower()
    assert len(gx10._store().list("pending")) == 1


def test_constraint_steering_surfaces_hard_floor_design_and_deviation_path(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, gate=True)
    gx10.record_constraints("Scope", "Python is required", language="python", network="none")
    gx10.record_design("Counter-proposal", "Use Rust instead", language="rust", network="allowed")

    block = gx10._steering_state_block()

    assert "- constraints: on record — HARD: language=python, network=none" in block
    assert "- design: language=rust, network=allowed (proposal)" in block
    assert "- to change a HARD constraint: record a counter-proposal design (record_design)" in block
    assert "do NOT re-negotiate in chat or re-call record_constraints" in block


def test_constraint_steering_surfaces_suggested_floor_as_advisory(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, gate=True)
    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", True)
    gx10.record_constraints("Scope", "Use Python", language="python", source="suggested")

    block = gx10._steering_state_block()

    assert "SUGGESTED" in block
    assert "typed floor" in block
    assert "/approve constraint <id|all>" in block
    assert "/dismiss constraint <id|all>" in block
    assert "BLOCKED" in block
    assert "NOT protecting" not in block
    assert "Do NOT treat this as safe" not in block
    assert "- constraints: on record — OK" not in block


def test_constraint_steering_suggested_match_shows_advisory_without_deviation(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, gate=True)
    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", True)
    gx10.record_constraints("Scope", "Use Python", language="python", source="suggested")
    gx10.record_design("Design", "Use Python", language="python")

    block = gx10._steering_state_block()

    assert "SUGGESTED" in block
    assert "BLOCKED" in block
    assert "/approve constraint" in block
    assert "DEVIATION" not in block
    assert "- constraints: on record — OK" not in block


def test_constraint_steering_suggested_detect_off_does_not_claim_blocked(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, gate=True)
    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", False)
    gx10.record_constraints("Scope", "Use Python", language="python", source="suggested")

    block = gx10._steering_state_block()

    assert "SUGGESTED" in block
    assert "advisory" in block
    assert "/approve constraint <id|all>" in block
    assert "/dismiss constraint <id|all>" in block
    assert "not enforced" in block
    assert "BLOCKED" not in block
    assert "NOT protecting" not in block
    assert "Do NOT treat this as safe" not in block
    assert "— OK" not in block


def test_constraint_steering_deviation_surfaces_suggested_mismatch(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, gate=True)
    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", True)
    gx10.record_constraints("Scope", "Use Python", language="python", source="suggested")
    gx10.record_design("Design", "Use Rust", language="rust")

    block = gx10._steering_state_block()

    assert "- constraints: DEVIATION — design language=rust contradicts / omits " in block
    assert "typed constraint language=python (advisory) — BLOCKED until resolved" in block
    assert "/approve constraint language" in block
    assert "/dismiss constraint language" in block


def test_constraint_steering_deviation_surfaces_suggested_omission(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, gate=True)
    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", True)
    gx10.record_constraints("Scope", "Use Python", language="python", source="suggested")
    gx10.record_design("Design", "No typed language")

    block = gx10._steering_state_block()

    assert "- constraints: DEVIATION — design language=omitted contradicts / omits " in block
    assert "typed constraint language=python (advisory) — BLOCKED until resolved" in block


def test_constraint_steering_additions_are_byte_identical_off(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, gate=True)
    gx10.record_constraints("Scope", "Python is required", language="python")
    gx10.record_design("Counter-proposal", "Use Rust instead", language="rust")
    monkeypatch.setattr(gx10, "CONSTRAINT_GATE_ENABLED", False)

    baseline = gx10._steering_state_block()
    monkeypatch.setattr(
        gx10,
        "_constraint_typed",
        lambda slug: (_ for _ in ()).throw(AssertionError("off path must not read typed constraints")),
    )

    assert gx10._steering_state_block() == baseline
    assert "HARD:" not in baseline
    assert "- design: language=" not in baseline
    assert "to change a HARD constraint" not in baseline


# --------------------------------------------------------------------------- #
# #1341: typed fields + hard/soft provenance + conflict-detect flag
# --------------------------------------------------------------------------- #


def test_record_constraints_language_alias_round_trip(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    gx10.record_constraints("Scope", "stay local", language="py")

    text = _doc().read_text(encoding="utf-8")
    assert "language: python\n" in text
    assert "source: hard\n" in text
    assert gx10._constraint_typed(gx10.active_slug()) == {"language": "python"}


def test_record_constraints_invalid_language_refuses(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    with pytest.raises(gx10.GateRefusal, match="invalid language value"):
        gx10.record_constraints("Scope", "stay local", language="klingon")
    assert not _doc().exists()
    assert gx10._constraint_typed(gx10.active_slug()) == {}


def test_record_constraints_no_typed_param_is_s1_identical(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    gx10.record_constraints("Scope floor", "stay local")
    text = _doc().read_bytes().decode("utf-8")
    assert text.startswith(
        "---\ntype: decision\nstage: constraints\ndeclared_none: false\ntitle: Scope floor\n---\n"
    )
    assert "language:" not in text
    assert "network:" not in text
    assert "source:" not in text


@pytest.mark.parametrize(
    ("body", "source", "expected"),
    [
        ("some prose only", "suggested", "source: suggested\n"),
        ("Constraints: keep it simple", "", "source: hard\n"),
    ],
)
def test_record_constraints_no_typed_detect_keeps_doc_level_source(monkeypatch, tmp_path,
                                                                   body, source, expected):
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", False)
    gx10.record_constraints("Scope", body, source=source)
    off_text = _doc().read_text(encoding="utf-8")

    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", True)
    gx10.record_constraints("Scope", body, source=source)
    on_text = _doc().read_text(encoding="utf-8")

    assert expected in on_text
    assert on_text == off_text


def test_record_constraints_suggested_excluded_from_typed_reader(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    gx10.record_constraints("Scope", "maybe python", language="py", source="suggested")
    text = _doc().read_text(encoding="utf-8")
    assert "language: python\n" in text
    assert "source: suggested\n" in text
    assert gx10._constraint_typed(gx10.active_slug()) == {}


def test_record_constraints_explicit_marker_is_hard(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    gx10.record_constraints("Scope", "Constraints: Python only\nno network", language="python")
    text = _doc().read_text(encoding="utf-8")
    assert "source: hard\n" in text
    assert gx10._constraint_typed(gx10.active_slug()) == {"language": "python"}


def test_constraint_tool_source_copy_is_detect_qualified():
    description = gx10.CONSTRAINT_TOOL["function"]["parameters"]["properties"]["source"]["description"]

    assert "conflict-detect on" in description
    assert "otherwise it is advisory" in description


def test_record_design_network_typed_round_trip(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    gx10.record_constraints("Scope", "stay local")  # presence ok when gate off
    rel = gx10.record_design("Approach", "use local cache", network="none")
    design = (gx10.vault_root() / gx10.active_slug() / "decisions" / "design.md")
    text = design.read_text(encoding="utf-8")
    assert "network: false\n" in text
    assert rel.endswith("decisions/design.md")


def test_record_design_no_typed_param_unchanged(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    gx10.record_design("Approach", "use Rust")
    text = (gx10.vault_root() / gx10.active_slug() / "decisions" / "design.md").read_text(encoding="utf-8")
    assert text.startswith(
        "---\ntype: proposal\nstage: design\napproved: false\ntitle: Approach\n---\n"
    )
    assert "language:" not in text
    assert "network:" not in text


def test_record_design_invalid_network_refuses(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    with pytest.raises(gx10.GateRefusal, match="invalid network value"):
        gx10.record_design("Approach", "body", network="maybe")
    assert not (gx10.vault_root() / gx10.active_slug() / "decisions" / "design.md").exists()


def test_constraint_typed_fail_soft_empty(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    assert gx10._constraint_typed(None) == {}
    assert gx10._constraint_typed(gx10.active_slug()) == {}


def test_constraint_dispatch_forwards_typed_params(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, gate=True)
    out = gx10.run_tool(
        "record_constraints",
        {"title": "Scope", "body": "stay local", "language": "py", "network": "none"},
    )
    assert out.startswith("OK: constraints recorded at ")
    assert gx10._constraint_typed(gx10.active_slug()) == {"language": "python", "network": False}


def test_design_dispatch_forwards_typed_params(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, gate=True)
    gx10.record_constraints("Scope", "stay local")
    out = gx10.run_tool(
        "record_design",
        {"title": "Approach", "body": "local only", "network": "forbidden"},
    )
    assert out.startswith("OK: design proposal recorded at ")
    text = (gx10.vault_root() / gx10.active_slug() / "decisions" / "design.md").read_text(encoding="utf-8")
    assert "network: false\n" in text


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, True),
        (False, False),
        (1, False),
        ("true", True),
        (" YES ", True),
        ("on", True),
        ("1", True),
        ("false", False),
        ("0", False),
        ("", False),
        ("garbage", False),
    ],
)
def test_constraint_conflict_detect_strict_bool(monkeypatch, value, expected):
    gx10._apply_constraint_conflict_detect({"safety": {"constraint_conflict_detect": value}})
    assert gx10.CONSTRAINT_CONFLICT_DETECT is expected


def test_constraint_conflict_detect_default_off_and_fail_soft(monkeypatch):
    gx10._apply_config(gx10._code_defaults())
    assert gx10.CONSTRAINT_CONFLICT_DETECT is False
    monkeypatch.setattr(gx10, "_cfg_get", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    gx10._apply_constraint_conflict_detect({})
    assert gx10.CONSTRAINT_CONFLICT_DETECT is False


def test_constraint_conflict_detect_off_is_byte_identical(monkeypatch, tmp_path):
    """Flag off: typed plumbing is dormant; S1 capture frontmatter + handover paths unchanged."""
    _setup(monkeypatch, tmp_path, gate=False)
    assert gx10.CONSTRAINT_CONFLICT_DETECT is False
    gx10.record_constraints("Scope floor", "stay local")
    text = _doc().read_bytes().decode("utf-8")
    assert text.startswith(
        "---\ntype: decision\nstage: constraints\ndeclared_none: false\ntitle: Scope floor\n---\n"
    )
    block = gx10._steering_state_block()
    assert "constraints:" not in block


# --------------------------------------------------------------------------- #
# #1364: an existing HARD typed floor cannot be silently overwritten
# --------------------------------------------------------------------------- #


def test_record_constraints_declared_none_ignores_typed_params(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", True)

    gx10.record_constraints("No constraints", "none", language="rust", network="none")

    text = _doc().read_text(encoding="utf-8")
    assert "declared_none: true\n" in text
    assert "language:" not in text and "network:" not in text and "source:" not in text
    assert gx10._constraint_typed(gx10.active_slug()) == {}


def test_record_constraints_refuses_german_language_body_without_typed_field(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, gate=True)

    with pytest.raises(gx10.GateRefusal) as exc:
        gx10.record_constraints("Scope", "Sprache: Python")

    msg = str(exc.value)
    assert "language requirement" in msg
    assert "Set the language field" in msg
    assert "language=python" not in msg
    assert not _doc().exists()

    gx10.record_constraints("Scope", "Sprache: Python", language="python")
    assert gx10._constraint_typed(gx10.active_slug()) == {"language": "python"}


@pytest.mark.parametrize(
    "body",
    [
        "implement in Rust",
        "coded in Go",
        "written in TypeScript",
    ],
)
def test_record_constraints_refuses_english_language_body_without_typed_field(
    monkeypatch, tmp_path, body
):
    _setup(monkeypatch, tmp_path, gate=True)

    with pytest.raises(gx10.GateRefusal) as exc:
        gx10.record_constraints("Scope", body)

    msg = str(exc.value)
    assert "language requirement" in msg
    assert "Set the language field" in msg
    assert not _doc().exists()


@pytest.mark.parametrize("body", ["keine externen Netzwerkzugriffe", "offline only", "muss offline"])
def test_record_constraints_refuses_network_body_without_typed_field(monkeypatch, tmp_path, body):
    _setup(monkeypatch, tmp_path, gate=True)

    with pytest.raises(gx10.GateRefusal) as exc:
        gx10.record_constraints("Scope", body)

    msg = str(exc.value)
    assert "network requirement" in msg
    assert "Set the network field" in msg
    assert "network=none" not in msg
    assert not _doc().exists()


@pytest.mark.parametrize(
    "body",
    [
        "keine externen Abhängigkeiten",
        "keine externen Bibliotheken",
        "keine externen Pakete",
        "the offline docs generator",
        "bring the node online",
    ],
)
def test_record_constraints_allows_incidental_network_words(monkeypatch, tmp_path, body):
    _setup(monkeypatch, tmp_path, gate=True)

    gx10.record_constraints("Scope", body)

    text = _doc().read_text(encoding="utf-8")
    assert "network:" not in text
    assert gx10._constraint_typed(gx10.active_slug()) == {}


def test_record_constraints_dependency_body_refuses_only_language(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, gate=True)
    body = "- Keine externen Abhängigkeiten (nur Python Standardbibliothek)\n- CLI-Tool"

    with pytest.raises(gx10.GateRefusal) as exc:
        gx10.record_constraints("Scope", body)

    msg = str(exc.value)
    assert "language requirement" in msg
    assert "Set the language field" in msg
    assert "network requirement" not in msg
    assert "network=none" not in msg
    assert not _doc().exists()


@pytest.mark.parametrize(
    "body",
    ["reads Python files", "Python tests", "Document Python-only examples"],
)
def test_record_constraints_allows_incidental_language_mentions(monkeypatch, tmp_path, body):
    _setup(monkeypatch, tmp_path, gate=True)

    gx10.record_constraints("Scope", body)

    assert _doc().is_file()
    assert gx10._constraint_typed(gx10.active_slug()) == {}


def test_record_constraints_capture_completeness_skips_typed_present_and_declared_none(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, gate=True)

    gx10.record_constraints("Scope", "written in TypeScript", language="typescript")
    assert gx10._constraint_typed(gx10.active_slug()) == {"language": "typescript"}

    none_root = tmp_path / "none"
    none_root.mkdir()
    _setup(monkeypatch, none_root, gate=True)
    gx10.record_constraints("No constraints", "none")
    assert gx10._constraint_status(gx10.active_slug()) == (gx10.CAPTURED_NONE, None)


def test_record_constraints_capture_completeness_gate_off_allows_prose_only(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, gate=False)

    gx10.record_constraints("Scope", "Sprache: Python")

    text = _doc().read_text(encoding="utf-8")
    assert "Sprache: Python" in text
    assert "language:" not in text


def test_record_constraints_refusal_recall_creates_floor_that_blocks_deviating_design(
    monkeypatch, tmp_path
):
    _setup(monkeypatch, tmp_path, gate=True)
    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", True)

    with pytest.raises(gx10.GateRefusal, match="Set the language field"):
        gx10.record_constraints("Scope", "Sprache: Python")

    gx10.record_constraints("Scope", "Sprache: Python", language="python")
    gx10.record_design("Design", "Use Rust instead", language="rust")

    approval = gx10._approve_design()
    assert approval.startswith("ERROR: pending constraint fork") or approval.startswith("ERROR: HARD constraint")
    assert "approved: false\n" in _design_doc().read_text(encoding="utf-8")


def test_record_constraints_refuses_hard_value_change_without_writing(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", True)
    gx10.record_constraints("Scope", "Python only", language="python")
    before = _doc().read_bytes()

    with pytest.raises(gx10.GateRefusal) as exc:
        gx10.record_constraints("Scope", "Rust instead", language="rust")

    message = str(exc.value)
    assert "language=python" in message
    assert "silently changed to rust" in message
    assert "record_design" in message and "/fork" in message
    assert _doc().read_bytes() == before


def test_record_constraints_allows_idempotent_hard_value(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", True)
    gx10.record_constraints("Scope", "Python only", language="python")

    gx10.record_constraints("Updated title", "Still Python", language="py")

    assert gx10._constraint_typed(gx10.active_slug()) == {"language": "python"}
    assert "title: Updated title\n" in _doc().read_text(encoding="utf-8")


def test_record_constraints_allows_new_typed_category(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", True)
    gx10.record_constraints("Scope", "Python only", language="python")

    gx10.record_constraints("Scope", "No network", network="none")

    assert gx10._constraint_typed(gx10.active_slug()) == {"language": "python", "network": False}


def test_record_constraints_prose_edit_preserves_hard_typed_value(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", True)
    gx10.record_constraints("Scope", "Python only", language="python")

    gx10.record_constraints("Clarified scope", "Python only, with standard tooling")

    assert gx10._constraint_typed(gx10.active_slug()) == {"language": "python"}
    assert _doc().read_text(encoding="utf-8").endswith("Python only, with standard tooling")


def test_record_constraints_declared_none_with_typed_change_preserves_hard_floor(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", True)
    gx10.record_constraints("Scope", "Python only", language="python")
    assert gx10._constraint_typed(gx10.active_slug()) == {"language": "python"}

    with pytest.raises(gx10.GateRefusal):
        gx10.record_constraints("No constraints", "none", language="rust")

    assert gx10._constraint_typed(gx10.active_slug()) == {"language": "python"}


def test_record_constraints_declared_none_cannot_clear_hard_floor(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", True)
    gx10.record_constraints("Scope", "Python only", language="python")

    with pytest.raises(gx10.GateRefusal) as exc:
        gx10.record_constraints("No constraints", "none")

    assert "record_design" in str(exc.value) and "/fork" in str(exc.value)
    assert gx10._constraint_typed(gx10.active_slug()) == {"language": "python"}


def test_record_constraints_operator_override_can_clear_hard_floor(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", True)
    gx10.record_constraints("Scope", "Python only", language="python")

    gx10.record_constraints("No constraints", "none", language="rust", source="operator-override")

    text = _doc().read_text(encoding="utf-8")
    assert "declared_none: true\n" in text
    assert "language:" not in text and "network:" not in text and "source:" not in text
    assert gx10._constraint_typed(gx10.active_slug()) == {}


def test_record_constraints_suggested_new_category_does_not_become_hard(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", True)
    gx10.record_constraints("Scope", "Python only", language="python")

    gx10.record_constraints("Scope", "No network suggested", network="none", source="suggested")

    assert gx10._constraint_typed(gx10.active_slug()) == {"language": "python"}
    assert gx10._constraint_typed_unresolved(gx10.active_slug()) == {"network": False}


def test_record_constraints_plain_resupply_preserves_suggested_category(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", True)
    gx10.record_constraints("Scope", "Python only", language="python")
    gx10.record_constraints("Scope", "No network suggested", network="none", source="suggested")

    gx10.record_constraints("Scope", "Plain resupply", language="python", network="none")

    assert gx10._constraint_typed(gx10.active_slug()) == {"language": "python"}
    assert gx10._constraint_typed_unresolved(gx10.active_slug()) == {"network": False}


def test_record_constraints_explicit_hard_resupply_promotes_suggested_category(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", True)
    gx10.record_constraints("Scope", "No network suggested", network="none", source="suggested")

    gx10.record_constraints("Scope", "Operator confirmed no network", network="none", source="hard")

    assert gx10._constraint_typed(gx10.active_slug()) == {"network": False}
    assert gx10._constraint_typed_unresolved(gx10.active_slug()) == {}


def test_record_constraints_changed_suggested_category_becomes_hard(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", True)
    gx10.record_constraints("Scope", "No network suggested", network="none", source="suggested")

    gx10.record_constraints("Scope", "Network now allowed", network="allowed")

    assert gx10._constraint_typed(gx10.active_slug()) == {"network": True}
    assert gx10._constraint_typed_unresolved(gx10.active_slug()) == {}


def test_record_constraints_plain_resupply_preserves_dismissed_category(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", True)
    gx10.record_constraints("Scope", "Use Python", language="python", source="suggested")
    assert gx10._dismiss_command("constraint language").startswith("OK: dismissed typed constraint")

    gx10.record_constraints("Scope", "Plain resupply", language="python")

    text = _doc().read_text(encoding="utf-8")
    assert "language: python\n" in text
    assert "source_language: dismissed\n" in text or "source: dismissed\n" in text
    assert gx10._constraint_typed(gx10.active_slug()) == {}
    assert gx10._constraint_typed_unresolved(gx10.active_slug()) == {}


def test_record_constraints_explicit_hard_resupply_rearms_dismissed_category(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", True)
    gx10.record_constraints("Scope", "Use Python", language="python", source="suggested")
    assert gx10._dismiss_command("constraint language").startswith("OK: dismissed typed constraint")

    gx10.record_constraints("Scope", "Operator confirmed Python", language="python", source="hard")

    text = _doc().read_text(encoding="utf-8")
    assert "language: python\n" in text
    assert "source_language: hard\n" in text or "source: hard\n" in text
    assert gx10._constraint_typed(gx10.active_slug()) == {"language": "python"}
    assert gx10._constraint_typed_unresolved(gx10.active_slug()) == {}


def test_record_constraints_omitted_dismissed_category_survives_and_can_be_approved(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", True)
    gx10.record_constraints("Scope", "Use Python", language="python", source="suggested")
    assert gx10._dismiss_command("constraint language").startswith("OK: dismissed typed constraint")

    gx10.record_constraints("Scope", "Add network", network="none")

    text = _doc().read_text(encoding="utf-8")
    assert "language: python\n" in text
    assert "source_language: dismissed\n" in text
    assert gx10._constraint_typed(gx10.active_slug()) == {"network": False}

    assert gx10._approve_command("constraint language").startswith("OK: approved constraint")
    assert gx10._constraint_typed(gx10.active_slug()) == {"language": "python", "network": False}


def test_record_constraints_operator_override_source_can_change_hard_value(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", True)
    gx10.record_constraints("Scope", "Python only", language="python")

    gx10.record_constraints(
        "Scope", "Operator chose Rust", language="rust", source="operator-override")

    text = _doc().read_text(encoding="utf-8")
    assert "language: rust\n" in text
    assert "source: operator-override\n" in text
    assert gx10._constraint_typed(gx10.active_slug()) == {"language": "rust"}


class TestConstraintTypedUnresolvedReader:
    def _setup_doc(self, monkeypatch, tmp_path, text, slug="reader-unit"):
        monkeypatch.setattr(gx10, "vault_root", lambda: tmp_path / "vault")
        doc = gx10.vault_root() / slug / "decisions" / "constraints.md"
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text(text, encoding="utf-8", newline="\n")
        return slug

    def test_reader_returns_suggested_typed_language(self, monkeypatch, tmp_path):
        slug = self._setup_doc(
            monkeypatch,
            tmp_path,
            "---\nsource: suggested\nlanguage: python\n---\nConstraints\n",
        )

        assert gx10._constraint_typed_unresolved(slug) == {"language": "python"}

    def test_reader_returns_suggested_typed_network(self, monkeypatch, tmp_path):
        slug = self._setup_doc(
            monkeypatch,
            tmp_path,
            "---\nsource: suggested\nnetwork: none\n---\nConstraints\n",
        )

        assert gx10._constraint_typed_unresolved(slug) == {"network": False}

    def test_reader_ignores_non_unresolved_sources(self, monkeypatch, tmp_path):
        cases = [
            "---\nsource: hard\nlanguage: python\n---\nConstraints\n",
            "---\nsource: dismissed\nlanguage: python\n---\nConstraints\n",
            "---\nlanguage: python\n---\nConstraints\n",
            "---\nsource: declared_none\nlanguage: none\nnetwork: none\n---\nConstraints\n",
            "---\nsource: suggested\n---\nConstraints\n",
            "Constraints without frontmatter\n",
        ]

        for i, text in enumerate(cases):
            slug = self._setup_doc(monkeypatch, tmp_path, text, slug=f"reader-case-{i}")
            assert gx10._constraint_typed_unresolved(slug) == {}

    def test_reader_ignores_oversized_doc(self, monkeypatch, tmp_path):
        slug = self._setup_doc(
            monkeypatch,
            tmp_path,
            "---\nsource: suggested\nlanguage: python\n---\n" + ("x" * 65537),
        )

        assert gx10._constraint_typed_unresolved(slug) == {}

    def test_reader_ignores_missing_doc(self, monkeypatch, tmp_path):
        monkeypatch.setattr(gx10, "vault_root", lambda: tmp_path / "vault")

        assert gx10._constraint_typed_unresolved("missing-reader-unit") == {}

    def test_reader_ignores_unreadable_payload(self, monkeypatch, tmp_path):
        monkeypatch.setattr(gx10, "vault_root", lambda: tmp_path / "vault")
        slug = "unreadable-reader-unit"
        doc = gx10.vault_root() / slug / "decisions" / "constraints.md"
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_bytes(b"\xff\xfe\xfd")

        assert gx10._constraint_typed_unresolved(slug) == {}

    def test_reader_and_hard_reader_are_mutually_exclusive(self, monkeypatch, tmp_path):
        cases = [
            (
                "---\nsource: suggested\nlanguage: python\n---\nConstraints\n",
                {},
                {"language": "python"},
            ),
            (
                "---\nsource: hard\nlanguage: python\n---\nConstraints\n",
                {"language": "python"},
                {},
            ),
            (
                "---\nlanguage: python\n---\nConstraints\n",
                {"language": "python"},
                {},
            ),
            (
                "---\nsource: dismissed\nlanguage: python\n---\nConstraints\n",
                {},
                {},
            ),
            (
                "---\nsource: declared_none\nlanguage: none\n---\nConstraints\n",
                {},
                {},
            ),
        ]

        for i, (text, expected_hard, expected_unresolved) in enumerate(cases):
            slug = self._setup_doc(monkeypatch, tmp_path, text, slug=f"exclusive-case-{i}")
            assert gx10._constraint_typed(slug) == expected_hard
            assert gx10._constraint_typed_unresolved(slug) == expected_unresolved
            assert set(gx10._constraint_typed(slug)).isdisjoint(gx10._constraint_typed_unresolved(slug))

    def test_reader_splits_per_category_sources(self, monkeypatch, tmp_path):
        slug = self._setup_doc(
            monkeypatch,
            tmp_path,
            "---\n"
            "language: python\n"
            "network: false\n"
            "source_language: suggested\n"
            "source_network: hard\n"
            "---\n"
            "Constraints\n",
        )

        assert gx10._constraint_typed(slug) == {"network": False}
        assert gx10._constraint_typed_unresolved(slug) == {"language": "python"}

    def test_reader_doc_level_fallback_still_applies(self, monkeypatch, tmp_path):
        slug = self._setup_doc(
            monkeypatch,
            tmp_path,
            "---\nsource: suggested\nlanguage: python\nnetwork: false\n---\nConstraints\n",
        )

        assert gx10._constraint_typed(slug) == {}
        assert gx10._constraint_typed_unresolved(slug) == {"language": "python", "network": False}

    def test_reader_per_category_source_overrides_doc_level(self, monkeypatch, tmp_path):
        slug = self._setup_doc(
            monkeypatch,
            tmp_path,
            "---\n"
            "source: suggested\n"
            "source_language: hard\n"
            "language: python\n"
            "network: false\n"
            "---\n"
            "Constraints\n",
        )

        assert gx10._constraint_typed(slug) == {"language": "python"}
        assert gx10._constraint_typed_unresolved(slug) == {"network": False}


def test_record_constraints_conflict_guard_is_off_gated(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", False)
    gx10.record_constraints("Scope", "Python only", language="python")

    gx10.record_constraints("Scope", "Rust instead", language="rust")

    assert gx10._constraint_typed(gx10.active_slug()) == {"language": "rust"}


def _design_doc() -> Path:
    return gx10.vault_root() / gx10.active_slug() / "decisions" / "design.md"


def _write_constraints(text: str) -> None:
    doc = _doc()
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text(text, encoding="utf-8", newline="\n")


def _stamp_design_approved() -> None:
    design = _design_doc()
    design.write_text(
        design.read_text(encoding="utf-8").replace("approved: false", "approved: true")
                                      .replace("type: proposal", "type: decision"),
        encoding="utf-8", newline="\n",
    )


def test_constraint_softcheck_lenient_matrix(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    slug = gx10.active_slug()
    _write_constraints(
        "---\n"
        "type: decision\n"
        "stage: constraints\n"
        "declared_none: false\n"
        "title: Scope\n"
        "language: python\n"
        "source: suggested\n"
        "---\n"
        "Use Python\n"
    )
    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", False)
    assert gx10._constraint_softcheck(slug, {}, require_present=True) is None
    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", True)

    missing = gx10._constraint_softcheck(slug, {}, require_present=True)
    assert missing and missing.startswith("ERROR:")
    assert "sets/omits language." in missing
    assert "sets/omits language (None)" not in missing
    assert "/dismiss constraint language" in missing
    assert "/approve constraint language" in missing
    mismatch = gx10._constraint_softcheck(slug, {"language": "rust"}, require_present=True)
    assert mismatch and "sets/omits language ('rust')" in mismatch
    assert gx10._constraint_softcheck(slug, {"language": "python"}, require_present=True) is None

    _write_constraints(_doc().read_text(encoding="utf-8").replace("source: suggested", "source: hard"))
    assert gx10._constraint_softcheck(slug, {"language": "rust"}, require_present=True) is None


def test_constraint_softcheck_fails_closed_on_internal_error(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, gate=True)
    slug = gx10.active_slug()
    gx10.record_constraints("Scope", "Use Python", language="python", source="suggested")
    gx10.record_design("Design", "Use Rust", language="rust")

    def boom(_slug):
        raise RuntimeError("reader failed")

    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", False)
    monkeypatch.setattr(gx10, "_constraint_typed_unresolved", boom)
    assert gx10._constraint_softcheck(slug, {"language": "rust"}, require_present=True) is None

    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", True)
    err = gx10._constraint_softcheck(slug, {"language": "rust"}, require_present=True)
    assert err is not None
    assert err.startswith("ERROR:")
    assert "fail-closed" in err

    out = gx10._approve_design()
    assert out == err
    assert "approved: false\n" in _design_doc().read_text(encoding="utf-8")


def test_approve_design_blocks_unresolved_suggested_deviation(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, gate=True)
    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", True)
    gx10.record_constraints("Scope", "Use Python", language="python", source="suggested")
    gx10.record_design("Design", "Use Rust", language="rust")

    out = gx10._approve_design()

    assert out.startswith("ERROR:")
    assert "/approve constraint language" in out
    assert "/dismiss constraint language" in out
    assert "approved: false\n" in _design_doc().read_text(encoding="utf-8")


def test_approve_design_allows_suggested_match_and_detect_off(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, gate=True)
    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", True)
    gx10.record_constraints("Scope", "Use Python", language="python", source="suggested")
    gx10.record_design("Design", "Use Python", language="python")
    assert gx10._approve_design().startswith("OK: approved the design")

    off_root = tmp_path / "off"
    off_root.mkdir()
    _setup(monkeypatch, off_root, gate=True)
    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", False)
    gx10.record_constraints("Scope", "Use Python", language="python", source="suggested")
    gx10.record_design("Design", "Use Rust", language="rust")
    assert gx10._approve_design().startswith("OK: approved the design")


def test_approve_design_pending_fork_wins_before_softcheck(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, gate=True)
    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", True)
    gx10.record_constraints("Scope", "Use Python", language="python")
    gx10.record_design("Design", "Use Rust", language="rust")
    _doc().write_text(_doc().read_text(encoding="utf-8").replace("source: hard", "source: suggested"),
                      encoding="utf-8", newline="\n")

    out = gx10._approve_design()

    assert out.startswith("ERROR: pending constraint fork")
    assert "/fork decide" in out


def test_dismiss_suggested_allows_later_approval(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, gate=True)
    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", True)
    gx10.record_constraints("Scope", "Use Python", language="python", source="suggested")
    gx10.record_design("Design", "Use Rust", language="rust")

    out = gx10._dismiss_constraint("all")

    assert out.startswith("OK: dismissed typed constraint")
    text = _doc().read_text(encoding="utf-8")
    assert "source: dismissed\n" in text
    assert gx10._constraint_typed(gx10.active_slug()) == {}
    assert gx10._constraint_typed_unresolved(gx10.active_slug()) == {}
    assert gx10._approve_design().startswith("OK: approved the design")
    assert gx10._dismiss_constraint("all").startswith("OK: constraint")


def test_dismiss_language_preserves_suggested_network(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, gate=True)
    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", True)
    gx10.record_constraints("Scope", "Use Python, no network", language="python", network="none",
                            source="suggested")

    out = gx10._dismiss_constraint("language")

    assert out.startswith("OK: dismissed")
    text = _doc().read_text(encoding="utf-8")
    assert "source_language: dismissed\n" in text
    assert gx10._constraint_typed(gx10.active_slug()) == {}
    assert gx10._constraint_typed_unresolved(gx10.active_slug()) == {"network": False}


def test_dismiss_network_refuses_when_only_network_is_hard(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _write_constraints(
        "---\n"
        "type: decision\n"
        "stage: constraints\n"
        "declared_none: false\n"
        "title: Scope\n"
        "language: python\n"
        "network: false\n"
        "source_language: suggested\n"
        "source_network: hard\n"
        "---\n"
        "Use Python, no network\n"
    )

    out = gx10._dismiss_constraint("network")

    assert out.startswith("ERROR: only source='suggested'")
    assert gx10._constraint_typed(gx10.active_slug()) == {"network": False}
    assert gx10._constraint_typed_unresolved(gx10.active_slug()) == {"language": "python"}


def test_dismiss_all_stamps_each_typed_category(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _write_constraints(
        "---\n"
        "type: decision\n"
        "stage: constraints\n"
        "declared_none: false\n"
        "title: Scope\n"
        "language: python\n"
        "network: false\n"
        "source_language: hard\n"
        "source_network: suggested\n"
        "---\n"
        "Use Python, no network\n"
    )

    out = gx10._dismiss_constraint("all")

    assert out.startswith("OK: dismissed")
    text = _doc().read_text(encoding="utf-8")
    assert "source_language: dismissed\n" in text
    assert "source_network: dismissed\n" in text
    assert gx10._constraint_typed(gx10.active_slug()) == {}
    assert gx10._constraint_typed_unresolved(gx10.active_slug()) == {}


def test_dismiss_all_reports_only_categories_changed_this_call(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _write_constraints(
        "---\n"
        "type: decision\n"
        "stage: constraints\n"
        "declared_none: false\n"
        "title: Scope\n"
        "language: python\n"
        "network: false\n"
        "source_language: dismissed\n"
        "source_network: suggested\n"
        "---\n"
        "Use Python, no network\n"
    )

    out = gx10._dismiss_constraint("all")

    assert out.endswith("(all: network).")
    assert "language" not in out
    text = _doc().read_text(encoding="utf-8")
    assert "source_language: dismissed\n" in text
    assert "source_network: dismissed\n" in text


@pytest.mark.parametrize("source", ["hard", "operator-override", "approved", "explicit", ""])
def test_dismiss_refuses_non_suggested_sources(monkeypatch, tmp_path, source):
    _setup(monkeypatch, tmp_path)
    source_line = f"source: {source}\n" if source else ""
    _write_constraints(
        "---\n"
        "type: decision\n"
        "stage: constraints\n"
        "declared_none: false\n"
        "title: Scope\n"
        "language: python\n"
        f"{source_line}"
        "---\n"
        "Use Python\n"
    )

    out = gx10._dismiss_constraint("language")

    assert out.startswith("ERROR: only source='suggested'")
    assert "Nothing changed" in out


def test_dismiss_errors_for_unknown_missing_and_no_unit(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    assert gx10._dismiss_constraint("runtime").startswith("ERROR: unknown constraint id")
    assert gx10._dismiss_constraint("language").startswith("ERROR: unit")
    monkeypatch.setattr(gx10, "active_slug", lambda: None)
    assert gx10._dismiss_constraint("language").startswith("ERROR: no active unit")


def test_promote_conflicting_approved_design_revokes_and_emits_fork(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, gate=True)
    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", True)
    gx10.record_constraints("Scope", "Use Python", language="python", source="suggested")
    gx10.record_design("Design", "Use Rust", language="rust")
    _stamp_design_approved()

    out = gx10._approve_constraint("language")

    assert "REVOKED" in out
    assert "source_language: hard\n" in _doc().read_text(encoding="utf-8")
    text = _design_doc().read_text(encoding="utf-8")
    assert "approved: false\n" in text
    assert "type: proposal\n" in text
    assert list((gx10.vault_root() / gx10.active_slug() / "proposals" / "forks").glob("*.json"))


def test_approve_dismissed_language_rearms_hard_and_blocks_deviating_design(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, gate=True)
    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", True)
    gx10.record_constraints("Scope", "Use Python", language="python", source="suggested")
    assert gx10._dismiss_constraint("language").startswith("OK: dismissed")
    assert gx10._constraint_typed(gx10.active_slug()) == {}

    out = gx10._approve_constraint("language")

    assert out.startswith("OK: approved")
    assert "Typed HARD floor is now active" in out
    assert gx10._constraint_typed(gx10.active_slug()) == {"language": "python"}
    gx10.record_design("Design", "Use Rust", language="rust")
    approval = gx10._approve_design()
    assert approval.startswith("ERROR: pending constraint fork") or approval.startswith("ERROR: HARD constraint")


def test_approve_constraint_language_promotes_only_language(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", True)
    gx10.record_constraints("Scope", "Use Python, no network", language="python", network="none",
                            source="suggested")

    out = gx10._approve_constraint("language")

    assert out.startswith("OK: approved")
    assert gx10._constraint_typed(gx10.active_slug()) == {"language": "python"}
    assert gx10._constraint_typed_unresolved(gx10.active_slug()) == {"network": False}


def test_approve_all_rearms_each_present_typed_category(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _write_constraints(
        "---\n"
        "type: decision\n"
        "stage: constraints\n"
        "declared_none: false\n"
        "title: Scope\n"
        "language: python\n"
        "network: false\n"
        "source_language: dismissed\n"
        "source_network: suggested\n"
        "---\n"
        "Use Python, no network\n"
    )

    out = gx10._approve_constraint("all")

    assert out.startswith("OK: approved")
    text = _doc().read_text(encoding="utf-8")
    assert "source_language: hard\n" in text
    assert "source_network: hard\n" in text
    assert gx10._constraint_typed(gx10.active_slug()) == {"language": "python", "network": False}


def test_promote_omission_revokes_without_fork(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, gate=True)
    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", True)
    gx10.record_constraints("Scope", "Use Python", language="python", source="suggested")
    gx10.record_design("Design", "No typed language here")
    _stamp_design_approved()

    out = gx10._approve_constraint("language")

    assert "REVOKED" in out
    assert "omission is not a counter-value" in out
    assert "approved: false\n" in _design_doc().read_text(encoding="utf-8")
    fork_dir = gx10.vault_root() / gx10.active_slug() / "proposals" / "forks"
    assert not fork_dir.exists() or not list(fork_dir.glob("*.json"))


def test_promote_match_and_detect_off_do_not_revoke(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, gate=True)
    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", True)
    gx10.record_constraints("Scope", "Use Python", language="python", source="suggested")
    gx10.record_design("Design", "Use Python", language="python")
    _stamp_design_approved()
    assert "REVOKED" not in gx10._approve_constraint("language")
    assert "approved: true\n" in _design_doc().read_text(encoding="utf-8")

    detect_off_root = tmp_path / "detect-off"
    detect_off_root.mkdir()
    _setup(monkeypatch, detect_off_root, gate=True)
    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", False)
    gx10.record_constraints("Scope", "Use Python", language="python", source="suggested")
    gx10.record_design("Design", "Use Rust", language="rust")
    _stamp_design_approved()
    assert "REVOKED" not in gx10._approve_constraint("language")
    assert "approved: true\n" in _design_doc().read_text(encoding="utf-8")


def test_already_hard_and_record_hard_revalidate_approved_design(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, gate=True)
    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", True)
    gx10.record_constraints("Scope", "Use Python", language="python")
    gx10.record_design("Design", "Use Rust", language="rust")
    _stamp_design_approved()
    out = gx10._approve_constraint("language")
    assert "REVOKED" in out
    assert "approved: false\n" in _design_doc().read_text(encoding="utf-8")

    record_hard_root = tmp_path / "record-hard"
    record_hard_root.mkdir()
    _setup(monkeypatch, record_hard_root, gate=True)
    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", True)
    gx10.record_constraints("Scope", "General constraints")
    gx10.record_design("Design", "Use Rust", language="rust")
    _stamp_design_approved()
    out = gx10.record_constraints("Scope", "Use Python", language="python")
    assert "REVOKED" in out
    assert "approved: false\n" in _design_doc().read_text(encoding="utf-8")


def test_revalidate_warning_includes_concrete_fork_hint(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, gate=True)
    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", True)
    gx10.record_constraints("Scope", "General constraints")
    gx10.record_design("Design", "Use Rust", language="rust")
    _stamp_design_approved()

    out = gx10.record_constraints("Scope", "Use Python", language="python")

    pending = gx10._pending_constraint_forks(gx10.active_slug())
    assert len(pending) == 1
    fid = pending[0].fork_id
    assert "WARNING:" in out
    assert fid in out
    assert f"/fork decide {fid} --choice keep" in out
    assert f"/fork decide {fid} --choice counter" in out


def test_revalidate_counter_promotion_requires_fresh_approval_softcheck(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, gate=True)
    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", True)
    gx10.record_constraints("Scope", "General constraints")
    gx10.record_design("Design", "Use Rust", language="rust")
    _stamp_design_approved()
    gx10.record_constraints("Scope", "Use Python", language="python")
    env = gx10._pending_constraint_forks(gx10.active_slug())[0]
    assert "approved: false\n" in _design_doc().read_text(encoding="utf-8")
    assert "approved: true\n" in env.counter_design

    out = gx10._fork_decide(env.fork_id, "counter")

    assert out.startswith("OK")
    promoted = _design_doc().read_text(encoding="utf-8")
    assert "approved: false\n" in promoted
    assert "type: proposal\n" in promoted
    calls = {"softcheck": 0}
    real_softcheck = gx10._constraint_softcheck

    def counted_softcheck(*args, **kwargs):
        calls["softcheck"] += 1
        return real_softcheck(*args, **kwargs)

    monkeypatch.setattr(gx10, "_constraint_softcheck", counted_softcheck)
    approve = gx10._approve_design()
    assert approve.startswith("OK")
    assert calls["softcheck"] == 1


def test_record_operator_override_revalidates_approved_design(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, gate=True)
    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", True)
    gx10.record_constraints("Scope", "Use Python", language="python")
    gx10.record_design("Design", "Use Python", language="python")
    _stamp_design_approved()

    out = gx10.record_constraints(
        "Scope", "Operator chose Rust", language="rust", source="operator-override")

    assert "REVOKED" in out
    assert "approved: false\n" in _design_doc().read_text(encoding="utf-8")


def test_record_suggested_warns_against_approved_design_without_revoke(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, gate=True)
    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", True)
    gx10.record_constraints("Scope", "General constraints")
    gx10.record_design("Design", "Use Python", language="python")
    _stamp_design_approved()

    out = gx10.record_constraints("Scope", "Try Rust", language="rust", source="suggested")

    assert out.startswith("vault/")
    assert "WARNING:" in out
    assert "language='rust'" in out
    assert "the design sets/omits language ('python')" in out
    assert "/dismiss constraint language" in out
    assert "/approve constraint language" in out
    assert "align the design" in out
    assert "REVOKED" not in out
    assert "approved: true\n" in _design_doc().read_text(encoding="utf-8")


def test_record_matching_suggested_after_approval_has_no_warning(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, gate=True)
    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", True)
    gx10.record_constraints("Scope", "General constraints")
    gx10.record_design("Design", "Use Python", language="python")
    _stamp_design_approved()

    out = gx10.record_constraints("Scope", "Still Python", language="python", source="suggested")

    assert out.startswith("vault/")
    assert "WARNING:" not in out
    assert "approved: true\n" in _design_doc().read_text(encoding="utf-8")


def test_record_suggested_postapproval_warning_is_detect_gated(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, gate=True)
    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", False)
    gx10.record_constraints("Scope", "General constraints")
    gx10.record_design("Design", "Use Python", language="python")
    _stamp_design_approved()

    out = gx10.record_constraints("Scope", "Try Rust", language="rust", source="suggested")

    assert out == "vault/demo/decisions/constraints.md"
    assert "approved: true\n" in _design_doc().read_text(encoding="utf-8")


def test_revalidate_approved_design_warns_on_internal_error(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, gate=True)
    slug = gx10.active_slug()
    gx10.record_constraints("Scope", "Use Python", language="python")
    gx10.record_design("Design", "Use Python", language="python")
    _stamp_design_approved()

    def boom(*_args, **_kwargs):
        raise RuntimeError("probe failed")

    monkeypatch.setattr(gx10, "_constraint_hardcheck", boom)
    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", False)
    assert gx10._revalidate_approved_design(slug) is None

    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", True)
    warning = gx10._revalidate_approved_design(slug)
    assert warning is not None
    assert warning.startswith("WARNING:")
    assert not warning.startswith("ERROR")
    assert "RuntimeError" in warning
    assert "nothing was auto-revoked" in warning
    assert "approved: true\n" in _design_doc().read_text(encoding="utf-8")


def test_fork_decide_counter_resolves_when_revalidation_warns(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, gate=True)
    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", True)
    gx10.record_constraints("Scope", "Python only, no network", language="python", network="none")
    gx10.record_design("Design", "Use Rust with network", language="rust", network="allowed")

    pending = gx10._pending_constraint_forks(gx10.active_slug())
    language_fork = next(env for env in pending if env.category == "language")

    def boom(*_args, **_kwargs):
        raise RuntimeError("probe failed")

    monkeypatch.setattr(gx10, "_constraint_hardcheck", boom)
    out = gx10._fork_decide(language_fork.fork_id, "counter")

    assert not out.startswith("ERROR")
    assert "choice=counter" in out
    assert "WARNING:" in out
    assert "RuntimeError" in out
    assert gx10._constraint_typed(gx10.active_slug()) == {"language": "rust", "network": False}
    resolved = gx10._find_fork_envelope(language_fork.fork_id, gx10.active_slug())
    assert resolved.status == "resolved"
    assert resolved.resolution["choice_id"] == "counter"
    assert not any(env.fork_id == language_fork.fork_id
                   for env in gx10._pending_constraint_forks(gx10.active_slug()))
