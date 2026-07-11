"""#1342 (S6 / epic #1344): L3 structured hard-check (fail-closed typed compare at impl boundary).

Pure ``hardcheck`` table + engine gate + approve / stage_handover / plan_units wiring under
``CONSTRAINT_CONFLICT_DETECT`` (default-off ⇒ byte-identical). Couples with S4 decide
(keep → design must match; counter → overridden constraint matches).
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from ack.ace.constraint_conflict import Violation, hardcheck

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=lambda **kw: object()))
_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402


# --------------------------------------------------------------------------- #
# Pure: hardcheck
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("constraint", "provided", "require_present", "expected_kind", "expected_category"),
    [
        # missing when required
        ({"language": "python"}, {}, True, "missing", "language"),
        ({"network": False}, {}, True, "missing", "network"),
        (
            {"language": "python", "network": False},
            {"language": "python"},
            True,
            "missing",
            "network",
        ),
        # mismatch
        ({"language": "python"}, {"language": "rust"}, True, "mismatch", "language"),
        ({"network": False}, {"network": True}, True, "mismatch", "network"),
        (
            {"language": "python", "network": False},
            {"language": "rust", "network": False},
            True,
            "mismatch",
            "language",  # TYPED_KEYS order
        ),
        (
            {"language": "python", "network": False},
            {"language": "python", "network": True},
            True,
            "mismatch",
            "network",
        ),
    ],
)
def test_hardcheck_violation(
    constraint, provided, require_present, expected_kind, expected_category
):
    v = hardcheck(constraint, provided, require_present=require_present)
    assert v is not None
    assert isinstance(v, Violation)
    assert v.kind == expected_kind
    assert v.category == expected_category
    assert v.required == constraint[expected_category]
    if expected_kind == "missing":
        assert v.provided is None
    else:
        assert v.provided == provided[expected_category]


@pytest.mark.parametrize(
    ("constraint", "provided", "require_present"),
    [
        ({}, {}, True),
        ({}, {"language": "rust"}, True),
        ({"language": "python"}, {"language": "python"}, True),
        ({"network": False}, {"network": False}, True),
        (
            {"language": "python", "network": False},
            {"language": "python", "network": False},
            True,
        ),
        # omission allowed when require_present=False (detect-like)
        ({"language": "python"}, {}, False),
        ({"language": "python"}, {"network": True}, False),
        # key only on provided side is fine
        ({}, {"language": "rust"}, False),
    ],
)
def test_hardcheck_none_when_match_or_no_floor(constraint, provided, require_present):
    assert hardcheck(constraint, provided, require_present=require_present) is None


def test_hardcheck_never_raises():
    assert hardcheck(None, {"language": "python"}, require_present=True) is None  # type: ignore[arg-type]
    assert hardcheck({"language": "python"}, "nope", require_present=True) is None  # type: ignore[arg-type]
    assert hardcheck("x", "y", require_present=True) is None  # type: ignore[arg-type]


def test_violation_is_frozen():
    v = Violation(category="language", required="python", provided=None, kind="missing")
    with pytest.raises(Exception):
        v.kind = "mismatch"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Engine harness
# --------------------------------------------------------------------------- #


def _setup(monkeypatch, tmp_path, *, detect=False, design_gate=False, constraint_gate=False):
    gx10._apply_config(gx10._code_defaults())
    monkeypatch.setattr(gx10, "DESIGN_GATE_ENABLED", design_gate)
    monkeypatch.setattr(gx10, "CONSTRAINT_GATE_ENABLED", constraint_gate)
    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", detect)
    gx10.STORE = None
    monkeypatch.setattr(gx10, "_ui_print", lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)
    gx10.initiative_new("Demo", "software")


def _slug() -> str:
    return gx10.active_slug()


def _design_doc() -> Path:
    return gx10.vault_root() / _slug() / "decisions" / "design.md"


def _impl_json(title="build it", **typed):
    payload = {
        "type": "implementation",
        "priority": "high",
        "title": title,
        "description": "x",
    }
    payload.update(typed)
    return json.dumps(payload)


def _stage(task_json, *, force=False):
    return gx10._stage_handover(None, "OPUS", "handover body", task_json, True, force)


def _pending():
    return gx10._store().list("pending")


def _fork_id() -> str:
    forks = list((gx10.vault_root() / _slug() / "proposals" / "forks").glob("*.json"))
    assert len(forks) == 1
    return json.loads(forks[0].read_text(encoding="utf-8"))["fork_id"]


# --------------------------------------------------------------------------- #
# _constraint_hardcheck unit
# --------------------------------------------------------------------------- #


def test_constraint_hardcheck_flag_off_is_none(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, detect=False)
    gx10.record_constraints("Scope", "Python only", language="python")
    assert gx10._constraint_hardcheck(_slug(), {}, require_present=True) is None
    assert gx10._constraint_hardcheck(
        _slug(), {"language": "rust"}, require_present=True
    ) is None


def test_constraint_hardcheck_no_hard_typed_is_none(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, detect=True)
    gx10.record_constraints("Scope", "prose only, no typed")
    assert gx10._constraint_typed(_slug()) == {}
    assert gx10._constraint_hardcheck(_slug(), {}, require_present=True) is None


def test_constraint_hardcheck_missing_and_mismatch(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, detect=True)
    gx10.record_constraints("Scope", "Python only", language="python")
    miss = gx10._constraint_hardcheck(_slug(), {}, require_present=True)
    assert miss is not None and miss.startswith("ERROR")
    assert "missing" in miss.lower() or "declare" in miss.lower()
    assert "python" in miss.lower()
    bad = gx10._constraint_hardcheck(_slug(), {"language": "rust"}, require_present=True)
    assert bad is not None and bad.startswith("ERROR")
    assert "rust" in bad.lower() and "python" in bad.lower()
    assert gx10._constraint_hardcheck(
        _slug(), {"language": "python"}, require_present=True
    ) is None


# --------------------------------------------------------------------------- #
# /approve design
# --------------------------------------------------------------------------- #


def test_approve_refuses_mismatch_and_omission(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, detect=True)
    gx10.record_constraints("Scope", "Python only", language="python")
    # omit typed language on design
    gx10.record_design("Approach", "use something")
    # no fork (design has no typed counter) — hardcheck still refuses omission
    out = gx10._approve_design()
    assert out.startswith("ERROR")
    assert "language" in out.lower()
    text = _design_doc().read_text(encoding="utf-8")
    assert "approved: true" not in text.replace(" ", "").lower()

    # mismatch: record with rust -> fork pending first blocks, then keep clears the rejected design
    gx10.record_design("Approach", "use Rust", language="rust")
    out_pending = gx10._approve_design()
    assert "pending constraint fork" in out_pending.lower()
    fid = _fork_id()
    assert gx10._fork_command(f"decide {fid} --choice keep").startswith("OK")
    out_keep = gx10._approve_design()
    assert out_keep.startswith("ERROR")
    assert "no design to approve" in out_keep.lower()
    assert not _design_doc().exists()


def test_approve_proceeds_after_counter_override(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, detect=True)
    gx10.record_constraints("Scope", "Python only", language="python")
    gx10.record_design("Approach", "use Rust", language="rust")
    fid = _fork_id()
    assert gx10._fork_command(f"decide {fid} --choice counter").startswith("OK")
    assert gx10._constraint_typed(_slug()).get("language") == "rust"
    out = gx10._approve_design()
    assert out.startswith("OK")
    fm = gx10._parse_frontmatter(_design_doc().read_text(encoding="utf-8"))
    assert fm.get("approved") == "true"
    assert fm.get("type") == "decision"


def test_approve_proceeds_after_keep_and_rerecord(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, detect=True)
    gx10.record_constraints("Scope", "Python only", language="python")
    gx10.record_design("Approach", "use Rust", language="rust")
    fid = _fork_id()
    assert gx10._fork_command(f"decide {fid} --choice keep").startswith("OK")
    assert gx10._constraint_typed(_slug()).get("language") == "python"
    # re-record design to match the floor
    gx10.record_design("Approach", "stay on Python", language="python")
    out = gx10._approve_design()
    assert out.startswith("OK")
    fm = gx10._parse_frontmatter(_design_doc().read_text(encoding="utf-8"))
    assert fm.get("approved") == "true"


# --------------------------------------------------------------------------- #
# impl stage_handover + plan_units
# --------------------------------------------------------------------------- #


def test_impl_handover_refuses_violation_and_omission_atomic(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, detect=True)
    gx10.record_constraints("Scope", "Python only", language="python")
    # omit language on task
    out = _stage(_impl_json("no lang"))
    assert out.startswith("ERROR")
    assert "language" in out.lower()
    assert _pending() == []
    # mismatch
    out2 = _stage(_impl_json("rust task", language="rust"))
    assert out2.startswith("ERROR")
    assert "rust" in out2.lower() or "python" in out2.lower()
    assert _pending() == []
    # match proceeds
    out3 = _stage(_impl_json("py task", language="python"))
    assert out3.startswith("OK")
    assert len(_pending()) == 1


def test_impl_handover_force_does_not_bypass(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, detect=True)
    gx10.record_constraints("Scope", "Python only", language="python")
    out = _stage(_impl_json("forced", language="rust"), force=True)
    assert out.startswith("ERROR")
    assert _pending() == []


def test_impl_rehandover_hardchecks_stored_typed(monkeypatch, tmp_path):
    """Re-hand of an impl task uses the stored task typed fields (closes #26)."""
    _setup(monkeypatch, tmp_path, detect=True)
    gx10.record_constraints("Scope", "Python only", language="python")
    assert _stage(_impl_json("ok", language="python")).startswith("OK")
    tid = _pending()[0]["id"]
    # Overwrite the stored task language to violate the floor (simulates drift / bad edit).
    p, _ = gx10._store()._find(tid)
    data = json.loads(p.read_text(encoding="utf-8"))
    data["language"] = "rust"
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    out = gx10._stage_handover(tid, "OPUS", "re-handover", None)
    assert out.startswith("ERROR")
    assert "language" in out.lower()


def test_plan_units_refuses_violation_atomic(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, detect=True)
    gx10.record_constraints("Scope", "Python only", language="python")
    epic = {"type": "epic", "priority": "high", "title": "Epic A", "description": "e"}
    units = [
        {
            "type": "implementation",
            "priority": "high",
            "title": "U1",
            "description": "u",
            "language": "rust",
        }
    ]
    out = gx10._plan_units(json.dumps(epic), json.dumps(units))
    assert out.startswith("ERROR")
    assert "language" in out.lower() or "rust" in out.lower()
    assert gx10._store().list() == []  # atomic


def test_plan_units_refuses_omission_atomic(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, detect=True)
    gx10.record_constraints("Scope", "Python only", language="python")
    epic = {"type": "epic", "priority": "high", "title": "Epic B", "description": "e"}
    units = [
        {"type": "implementation", "priority": "high", "title": "U1", "description": "u"}
    ]
    out = gx10._plan_units(json.dumps(epic), json.dumps(units))
    assert out.startswith("ERROR")
    assert "missing" in out.lower() or "declare" in out.lower() or "language" in out.lower()
    assert gx10._store().list() == []


def test_plan_units_force_does_not_bypass(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, detect=True)
    gx10.record_constraints("Scope", "Python only", language="python")
    epic = {"type": "epic", "priority": "high", "title": "Epic C", "description": "e"}
    units = [
        {
            "type": "implementation",
            "priority": "high",
            "title": "U1",
            "description": "u",
            "language": "rust",
        }
    ]
    out = gx10._plan_units(json.dumps(epic), json.dumps(units), force=True)
    assert out.startswith("ERROR")
    assert gx10._store().list() == []


def test_plan_units_match_proceeds(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, detect=True)
    gx10.record_constraints("Scope", "Python only", language="python")
    epic = {"type": "epic", "priority": "high", "title": "Epic D", "description": "e"}
    units = [
        {
            "type": "implementation",
            "priority": "high",
            "title": "U1",
            "description": "u",
            "language": "python",
        }
    ]
    out = gx10._plan_units(json.dumps(epic), json.dumps(units))
    assert out.startswith("OK") or "created" in out.lower() or "unit" in out.lower()
    assert not out.startswith("ERROR")
    assert len(gx10._store().list()) >= 2  # epic + unit


def test_non_impl_handover_unaffected(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, detect=True)
    gx10.record_constraints("Scope", "Python only", language="python")
    tj = json.dumps(
        {
            "type": "architecture",
            "priority": "high",
            "title": "design it",
            "description": "x",
        }
    )
    out = _stage(tj)
    assert "refused" not in out.lower()
    assert not out.startswith("ERROR: HARD constraint")
    assert len(_pending()) == 1


def test_record_design_stays_advisory(monkeypatch, tmp_path):
    """Conflicting design is recorded + forked; hard-refuse is NOT at record_design."""
    _setup(monkeypatch, tmp_path, detect=True)
    gx10.record_constraints("Scope", "Python only", language="python")
    rel = gx10.record_design("Approach", "use Rust", language="rust")
    assert rel.endswith("decisions/design.md")
    assert _design_doc().is_file()
    forks = list((gx10.vault_root() / _slug() / "proposals" / "forks").glob("*.json"))
    assert len(forks) == 1
    # approval is the hard floor
    assert gx10._approve_design().startswith("ERROR")


# --------------------------------------------------------------------------- #
# Byte-identical default-off
# --------------------------------------------------------------------------- #


def test_flag_off_byte_identical_no_hardcheck(monkeypatch, tmp_path):
    """Flag OFF → approve / impl handover / plan_units behave as S3–S5 (no hardcheck)."""
    _setup(monkeypatch, tmp_path, detect=False)
    assert gx10.CONSTRAINT_CONFLICT_DETECT is False
    # Unit helper short-circuits without reading the floor.
    assert gx10._constraint_hardcheck("any", {}, require_present=True) is None

    gx10.record_constraints("Scope", "Python only", language="python")
    gx10.record_design("Approach", "use Rust", language="rust")
    # approve proceeds (no pending fork when detect off; no hardcheck)
    out = gx10._approve_design()
    assert out.startswith("OK")
    # mismatched task language is allowed (no hardcheck)
    out2 = _stage(_impl_json("free", language="rust"))
    assert out2.startswith("OK")
    assert len(_pending()) == 1
    # plan_units with mismatch allowed
    epic = {"type": "epic", "priority": "high", "title": "Epic Off", "description": "e"}
    units = [
        {
            "type": "implementation",
            "priority": "high",
            "title": "U-off",
            "description": "u",
            "language": "go",
        }
    ]
    out3 = gx10._plan_units(json.dumps(epic), json.dumps(units))
    assert not out3.startswith("ERROR: HARD constraint")
    assert not out3.startswith("ERROR")


def test_pure_module_boundary_clean():
    """Boundary: hardcheck lives in pure ack (no engine import)."""
    src = (
        Path(__file__).resolve().parents[1] / "ace" / "constraint_conflict.py"
    ).read_text(encoding="utf-8")
    assert "import gx10" not in src
    assert "from engine" not in src
    assert "import engine" not in src
    assert "def hardcheck" in src
    assert "class Violation" in src
