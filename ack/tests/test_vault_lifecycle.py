from __future__ import annotations

import sys
import types
import json
from pathlib import Path

import pytest

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=lambda **kw: object()))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import project_context as pc
from project_context import ProjectContext
import gx10


def test_lifecycle_stages_canonical_order():
    assert gx10.LIFECYCLE_STAGES == (
        "idea",
        "design",
        "adr",
        "spec",
        "tests",
        "proposals",
        "reviews",
        "delivery",
    )


def test_is_lifecycle_stage():
    assert gx10.is_lifecycle_stage("spec")
    assert gx10.is_lifecycle_stage("delivery")
    assert not gx10.is_lifecycle_stage("bogus")
    assert not gx10.is_lifecycle_stage("")


def test_can_advance_forward_and_same():
    assert gx10.can_advance_stage("idea", "spec")
    assert gx10.can_advance_stage("spec", "spec")


def test_can_advance_backward_refused_unless_regress():
    assert not gx10.can_advance_stage("spec", "idea")
    assert gx10.can_advance_stage("spec", "idea", allow_regress=True)


def test_can_advance_empty_from_admits_any_valid():
    assert gx10.can_advance_stage("", "idea")
    assert gx10.can_advance_stage("", "delivery")
    assert not gx10.can_advance_stage("", "bogus")


def test_can_advance_unknown_refused():
    assert not gx10.can_advance_stage("idea", "bogus")
    assert not gx10.can_advance_stage("bogus", "spec")


def test_can_advance_non_str_from_refused():
    # only the empty STRING admits any valid target; falsy non-str (None / []) must NOT slip through
    assert not gx10.can_advance_stage(None, "spec")
    assert not gx10.can_advance_stage([], "spec")
    assert not gx10.can_advance_stage("idea", None)


def test_lifecycle_state_empty():
    st = gx10.lifecycle_state([])
    assert st["present"] == []
    assert st["current"] == ""
    assert st["complete"] is False
    assert st["gaps"] == []


def test_lifecycle_state_composes_present_current_gaps():
    docs = [
        {"stage": "design"},
        {"stage": "spec"},
        {"stage": "spec"},
        {"stage": "nonsense"},
        {"stage": ""},
    ]
    st = gx10.lifecycle_state(docs)
    assert st["present"] == ["design", "spec"]
    assert st["current"] == "spec"
    assert st["gaps"] == ["idea", "adr"]
    assert st["complete"] is False
    assert st["counts"] == {"design": 1, "spec": 2}
    assert st["unknown"] == ["nonsense"]


def test_lifecycle_state_complete_when_no_gaps():
    docs = [{"stage": "idea"}, {"stage": "design"}]
    st = gx10.lifecycle_state(docs)
    assert st["current"] == "design"
    assert st["gaps"] == []
    assert st["complete"] is True


def test_reconcile_graph_carries_lifecycle_and_node_stage(tmp_path):
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        v = gx10.initiative_new("LC Demo", "software")
        vdir = v.path
        (vdir / "design.md").write_text(
            "---\ntitle: D\nstage: design\n---\nx\n", encoding="utf-8"
        )
        (vdir / "spec.md").write_text(
            "---\ntitle: S\nstage: spec\n---\nx\n", encoding="utf-8"
        )
        gx10.reconcile_vault(v.slug, links=True)
        g = json.loads((vdir / "GRAPH.json").read_text(encoding="utf-8"))
        assert g["lifecycle"]["current"] == "spec"
        assert "idea" in g["lifecycle"]["gaps"]
        assert g["nodes"]["spec.md"]["stage"] == "spec"


def test_reconcile_lifecycle_md_has_summary(tmp_path):
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        v = gx10.initiative_new("LC Demo", "software")
        vdir = v.path
        (vdir / "design.md").write_text(
            "---\ntitle: D\nstage: design\n---\nx\n", encoding="utf-8"
        )
        (vdir / "spec.md").write_text(
            "---\ntitle: S\nstage: spec\n---\nx\n", encoding="utf-8"
        )
        gx10.reconcile_vault(v.slug, links=True)
        lifecycle_md = (vdir / "LIFECYCLE.md").read_text(encoding="utf-8")
        assert "## Lifecycle" in lifecycle_md
        assert "## Graph" in lifecycle_md


def test_reconcile_lifecycle_idempotent(tmp_path):
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        v = gx10.initiative_new("LC Demo", "software")
        vdir = v.path
        (vdir / "design.md").write_text(
            "---\ntitle: D\nstage: design\n---\nx\n", encoding="utf-8"
        )
        (vdir / "spec.md").write_text(
            "---\ntitle: S\nstage: spec\n---\nx\n", encoding="utf-8"
        )
        gx10.reconcile_vault(v.slug, links=True)
        graph_bytes_1 = (vdir / "GRAPH.json").read_bytes()
        lifecycle_bytes_1 = (vdir / "LIFECYCLE.md").read_bytes()
        gx10.reconcile_vault(v.slug, links=True)
        graph_bytes_2 = (vdir / "GRAPH.json").read_bytes()
        lifecycle_bytes_2 = (vdir / "LIFECYCLE.md").read_bytes()
        assert graph_bytes_1 == graph_bytes_2
        assert lifecycle_bytes_1 == lifecycle_bytes_2
