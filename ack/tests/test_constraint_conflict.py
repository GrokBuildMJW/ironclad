"""#1337 (S3 / epic #1344): structured constraint-conflict detector + durable fork-envelope emission.

Pure detector + envelope identity/round-trip, plus engine emission E2E under
``CONSTRAINT_CONFLICT_DETECT`` (default-off ⇒ byte-identical; no MPR / no /fork).
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from ack.ace.constraint_conflict import Conflict, detect_conflict
from ack.ace.fork_envelope import (
    ForkEnvelope,
    build_constraint_envelope,
    make_fork_id,
)

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=lambda **kw: object()))
_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402


# --------------------------------------------------------------------------- #
# Pure: detect_conflict
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("constraint", "design", "expected_category"),
    [
        ({"language": "python"}, {"language": "rust"}, "language"),
        ({"network": False}, {"network": True}, "network"),
        (
            {"language": "python", "network": False},
            {"language": "rust", "network": True},
            "language",  # TYPED_KEYS order: language first
        ),
        (
            {"language": "python", "network": False},
            {"language": "python", "network": True},
            "network",  # language matches; network differs
        ),
    ],
)
def test_detect_conflict_mismatch(constraint, design, expected_category):
    c = detect_conflict(constraint, design)
    assert c is not None
    assert c.category == expected_category
    assert c.required == constraint[expected_category]
    assert c.counter == design[expected_category]


@pytest.mark.parametrize(
    ("constraint", "design"),
    [
        ({}, {}),
        ({"language": "python"}, {}),
        ({}, {"language": "rust"}),
        ({"language": "python"}, {"language": "python"}),
        ({"network": False}, {"network": False}),
        (
            {"language": "python", "network": False},
            {"language": "python", "network": False},
        ),
        # key only on one side of each pair → no shared differing key
        ({"language": "python"}, {"network": True}),
        ({"network": False}, {"language": "rust"}),
    ],
)
def test_detect_conflict_none_when_match_or_absent(constraint, design):
    assert detect_conflict(constraint, design) is None


def test_detect_conflict_never_raises():
    assert detect_conflict(None, {"language": "python"}) is None  # type: ignore[arg-type]
    assert detect_conflict({"language": "python"}, "nope") is None  # type: ignore[arg-type]
    assert detect_conflict("x", "y") is None  # type: ignore[arg-type]


def test_conflict_is_frozen():
    c = Conflict(category="language", required="python", counter="rust")
    with pytest.raises(Exception):
        c.category = "network"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Pure: make_fork_id + envelope
# --------------------------------------------------------------------------- #


def test_make_fork_id_stable_and_opaque():
    a = make_fork_id("unit-a", "language", "crev1", "drev1", ["keep", "counter"])
    b = make_fork_id("unit-a", "language", "crev1", "drev1", ["counter", "keep"])  # sorted
    assert a == b
    assert len(a) == 16
    assert all(c in "0123456789abcdef" for c in a)
    # no issue-number formatting
    assert "#" not in a


def test_make_fork_id_excludes_question_and_distinguishes_inputs():
    """Identity is slug|category|revs|option_ids only — free-text question is out of band."""
    base = make_fork_id("s", "language", "c1", "d1", ["keep", "counter"])
    # Different revs / category / slug change the id
    assert make_fork_id("s", "language", "c2", "d1", ["keep", "counter"]) != base
    assert make_fork_id("s", "language", "c1", "d2", ["keep", "counter"]) != base
    assert make_fork_id("s", "network", "c1", "d1", ["keep", "counter"]) != base
    assert make_fork_id("other", "language", "c1", "d1", ["keep", "counter"]) != base
    # Option-id set matters
    assert make_fork_id("s", "language", "c1", "d1", ["keep"]) != base


def test_build_constraint_envelope_and_round_trip():
    conflict = Conflict(category="language", required="python", counter="rust")
    env = build_constraint_envelope(
        mem_ns="ns1",
        slug="demo",
        conflict=conflict,
        constraint_rev="crev",
        design_rev="drev",
        counter_design="counter body",
        restore_design="restore body",
    )
    assert env.area == "constraint"
    assert env.category == "language"
    assert env.status == "pending"
    assert env.recommendation is None
    assert env.matrix is None
    assert env.resolution is None
    assert env.mem_ns == "ns1" and env.slug == "demo"
    assert env.counter_design == "counter body"
    assert env.restore_design == "restore body"
    assert env.question == "required language=python vs proposed rust"
    assert [o["id"] for o in env.options] == ["keep", "counter"]
    assert env.options[0]["value"] == "python"
    assert env.options[1]["value"] == "rust"
    assert env.fork_id == make_fork_id(
        "demo", "language", "crev", "drev", ["keep", "counter"]
    )

    restored = ForkEnvelope.from_dict(env.to_dict())
    assert restored.to_dict() == env.to_dict()
    # JSON round-trip
    payload = json.loads(json.dumps(env.to_dict()))
    assert ForkEnvelope.from_dict(payload).to_dict() == env.to_dict()
    without_bodies = build_constraint_envelope(
        mem_ns="ns1",
        slug="demo",
        conflict=conflict,
        constraint_rev="crev",
        design_rev="drev",
    )
    assert without_bodies.fork_id == env.fork_id


def test_fork_envelope_from_dict_drift_tolerant():
    thin = ForkEnvelope.from_dict({"fork_id": "abc", "slug": "x"})
    assert thin.fork_id == "abc" and thin.slug == "x"
    assert thin.status == "pending" and thin.area == "constraint"
    assert thin.counter_design is None and thin.restore_design is None
    drift = ForkEnvelope.from_dict({"fork_id": "abc", "counter_design": 7, "restore_design": []})
    assert drift.counter_design is None and drift.restore_design is None
    assert ForkEnvelope.from_dict(None).fork_id == ""
    assert ForkEnvelope.from_dict("garbage").fork_id == ""


# --------------------------------------------------------------------------- #
# Engine emission E2E
# --------------------------------------------------------------------------- #


def _setup(monkeypatch, tmp_path, *, detect=False, gate=False):
    gx10._apply_config(gx10._code_defaults())
    monkeypatch.setattr(gx10, "DESIGN_GATE_ENABLED", False)
    monkeypatch.setattr(gx10, "CONSTRAINT_GATE_ENABLED", gate)
    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", detect)
    gx10.STORE = None
    monkeypatch.setattr(gx10, "_ui_print", lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)
    gx10.initiative_new("Demo", "software")


def _slug() -> str:
    return gx10.active_slug()


def _forks_dir() -> Path:
    return gx10.vault_root() / _slug() / "proposals" / "forks"


def test_emission_on_language_conflict(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, detect=True)
    gx10.record_constraints("Scope", "Python only", language="python")
    gx10.record_design("Approach", "use Rust", language="rust")

    forks = list(_forks_dir().glob("*.json"))
    assert len(forks) == 1
    data = json.loads(forks[0].read_text(encoding="utf-8"))
    assert data["status"] == "pending"
    assert data["area"] == "constraint"
    assert data["category"] == "language"
    assert data["recommendation"] is None
    assert data["matrix"] is None
    assert [o["id"] for o in data["options"]] == ["keep", "counter"]
    assert data["options"][0]["value"] == "python"
    assert data["options"][1]["value"] == "rust"
    assert data["slug"] == _slug()
    assert forks[0].name == f"{data['fork_id']}.json"
    assert "#" not in data["fork_id"]

    loaded = gx10._load_fork_envelopes(_slug())
    assert len(loaded) == 1
    assert loaded[0].fork_id == data["fork_id"]


def test_emission_none_when_typed_match(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, detect=True)
    gx10.record_constraints("Scope", "Python only", language="python")
    gx10.record_design("Approach", "stay on Python", language="python")
    assert not _forks_dir().exists() or list(_forks_dir().glob("*.json")) == []


def test_emission_none_when_no_typed_on_design(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, detect=True)
    gx10.record_constraints("Scope", "Python only", language="python")
    gx10.record_design("Approach", "use whatever")
    assert not _forks_dir().exists() or list(_forks_dir().glob("*.json")) == []


def test_emission_idempotent_re_record(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, detect=True)
    gx10.record_constraints("Scope", "Python only", language="python")
    gx10.record_design("Approach", "use Rust", language="rust")
    first = list(_forks_dir().glob("*.json"))
    assert len(first) == 1
    first_id = first[0].name
    first_bytes = first[0].read_bytes()

    # Same conflict + same design content → same revs → same fork_id → one file
    gx10.record_design("Approach", "use Rust", language="rust")
    again = list(_forks_dir().glob("*.json"))
    assert len(again) == 1
    assert again[0].name == first_id
    # constraints.md unchanged → same envelope content (idempotent overwrite)
    assert again[0].read_bytes() == first_bytes


def test_flag_off_byte_identical_no_ledger(monkeypatch, tmp_path):
    """Flag OFF → record_design is S5-identical: no detect, no forks ledger dir/files."""
    _setup(monkeypatch, tmp_path, detect=False)
    assert gx10.CONSTRAINT_CONFLICT_DETECT is False

    # Guard: emission helpers must not run on the off path.
    monkeypatch.setattr(
        gx10,
        "_emit_constraint_conflict_envelope",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("off path must not emit")),
    )
    monkeypatch.setattr(
        gx10,
        "_constraint_typed",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("off path must not read typed")),
    )

    gx10.record_constraints("Scope", "Python only", language="python")
    gx10.record_design("Approach", "use Rust", language="rust")

    design = gx10.vault_root() / _slug() / "decisions" / "design.md"
    text = design.read_text(encoding="utf-8")
    assert text.startswith(
        "---\ntype: proposal\nstage: design\napproved: false\ntitle: Approach\nlanguage: rust\n---\n"
    )
    forks = gx10.vault_root() / _slug() / "proposals" / "forks"
    assert not forks.exists()
    assert gx10._load_fork_envelopes(_slug()) == []


def test_emission_fail_soft_does_not_break_record_design(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, detect=True)
    gx10.record_constraints("Scope", "Python only", language="python")
    monkeypatch.setattr(
        gx10,
        "_emit_constraint_conflict_envelope",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    rel = gx10.record_design("Approach", "use Rust", language="rust")
    assert rel.endswith("decisions/design.md")
    assert (gx10.vault_root() / _slug() / "decisions" / "design.md").is_file()


def test_pure_modules_have_no_engine_import():
    """Boundary: constraint_conflict + fork_envelope are pure ack (no engine import)."""
    root = Path(__file__).resolve().parents[1] / "ace"
    for name in ("constraint_conflict.py", "fork_envelope.py"):
        src = (root / name).read_text(encoding="utf-8")
        assert "import gx10" not in src
        assert "from engine" not in src
        assert "import engine" not in src
