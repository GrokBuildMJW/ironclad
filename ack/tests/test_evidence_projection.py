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

TS = "abc123def456abc123def456"


def test_project_evidence_writes_tagged_doc(tmp_path):
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        v = gx10.initiative_new("Demo", "software")
        rel = gx10.project_evidence("design", "Design note", "We chose X.", tree_sha=TS)
        doc = Path(str(tmp_path)) / "vault" / rel
        assert doc.is_file()
        txt = doc.read_text(encoding="utf-8")
        assert "type: evidence" in txt
        assert "stage: design" in txt
        assert f"tree_sha: {TS}" in txt
        assert "content_hash:" in txt
        assert "design" in rel
        assert "evidence/" in rel


def test_project_evidence_idempotent(tmp_path):
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        v = gx10.initiative_new("Demo", "software")
        a = gx10.project_evidence("design", "D", "body", tree_sha=TS)
        b = gx10.project_evidence("design", "D", "body", tree_sha=TS)
        assert a == b
        files = list((v.path / "evidence").glob("*.md"))
        assert len(files) == 1


def test_project_evidence_crlf_idempotent(tmp_path):
    # CRLF input must normalize so a re-projection is a no-op (read_text returns LF)
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        v = gx10.initiative_new("Demo", "software")
        a = gx10.project_evidence("design", "D", "line1\r\nline2", tree_sha=TS)
        b = gx10.project_evidence("design", "D", "line1\r\nline2", tree_sha=TS)
        assert a == b
        assert len(list((v.path / "evidence").glob("*.md"))) == 1


def test_project_evidence_unsafe_tree_sha_or_hash_rejected(tmp_path):
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        gx10.initiative_new("Demo", "software")
        for bad in ("../evil", "abc/def", "a b", "zz!!"):
            with pytest.raises(ValueError):
                gx10.project_evidence("design", "D", "b", tree_sha=bad)
        with pytest.raises(ValueError):
            gx10.project_evidence("design", "D", "b", tree_sha=TS, content_hash="../x")


def test_project_evidence_same_body_different_title_two_files(tmp_path):
    # append-only: a different title => a distinct evidence file (title is in the filename identity)
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        v = gx10.initiative_new("Demo", "software")
        gx10.project_evidence("design", "Title A", "body", tree_sha=TS)
        gx10.project_evidence("design", "Title B", "body", tree_sha=TS)
        assert len(list((v.path / "evidence").glob("*.md"))) == 2


def test_project_evidence_different_body_new_file(tmp_path):
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        v = gx10.initiative_new("Demo", "software")
        gx10.project_evidence("design", "D", "body one", tree_sha=TS)
        gx10.project_evidence("design", "D", "body two", tree_sha=TS)
        files = list((v.path / "evidence").glob("*.md"))
        assert len(files) == 2


def test_project_evidence_unknown_stage_rejected(tmp_path):
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        v = gx10.initiative_new("Demo", "software")
        with pytest.raises(ValueError):
            gx10.project_evidence("bogus", "D", "b", tree_sha=TS)


def test_project_evidence_empty_tree_sha_rejected(tmp_path):
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        v = gx10.initiative_new("Demo", "software")
        with pytest.raises(ValueError):
            gx10.project_evidence("design", "D", "b", tree_sha="")


def test_project_evidence_no_active_initiative_rejected(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert pc.current() is None
    with pytest.raises(ValueError):
        gx10.project_evidence("design", "D", "b", tree_sha=TS)


def test_lifecycle_completeness_ready(tmp_path):
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        v = gx10.initiative_new("Demo", "software")
        gx10.project_evidence("design", "D", "x", tree_sha=TS)
        gx10.project_evidence("tests", "T", "y", tree_sha=TS)
        ready, reasons = gx10.lifecycle_completeness(
            v.slug, required_stages=["design", "tests"], tree_sha=TS
        )
        assert ready
        assert reasons == []


def test_lifecycle_completeness_missing_stage(tmp_path):
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        v = gx10.initiative_new("Demo", "software")
        gx10.project_evidence("design", "D", "x", tree_sha=TS)
        ready, reasons = gx10.lifecycle_completeness(
            v.slug, required_stages=["design", "adr"], tree_sha=TS
        )
        assert not ready
        assert any("adr" in r for r in reasons)


def test_lifecycle_completeness_tree_sha_mismatch(tmp_path):
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        v = gx10.initiative_new("Demo", "software")
        gx10.project_evidence("design", "D", "x", tree_sha=TS)
        ready, reasons = gx10.lifecycle_completeness(
            v.slug, required_stages=["design"], tree_sha="OTHER"
        )
        assert not ready
        assert any("tree_sha" in r for r in reasons)


def test_lifecycle_completeness_empty_tree_sha_fail_closed(tmp_path):
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        v = gx10.initiative_new("Demo", "software")
        ready, reasons = gx10.lifecycle_completeness(
            v.slug, required_stages=["design"], tree_sha=""
        )
        assert not ready
        assert reasons


def test_lifecycle_completeness_unknown_required_stage(tmp_path):
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        v = gx10.initiative_new("Demo", "software")
        gx10.project_evidence("design", "D", "x", tree_sha=TS)
        ready, reasons = gx10.lifecycle_completeness(
            v.slug, required_stages=["bogus"], tree_sha=TS
        )
        assert not ready
        assert any("bogus" in r for r in reasons)


def test_lifecycle_completeness_unknown_initiative_fail_closed(tmp_path):
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        ready, reasons = gx10.lifecycle_completeness(
            "does-not-exist", required_stages=["design"], tree_sha=TS
        )
        assert not ready
        assert reasons


def test_evidence_doc_in_lifecycle_state_and_graph(tmp_path):
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        v = gx10.initiative_new("Demo", "software")
        gx10.project_evidence("design", "D", "x", tree_sha=TS)
        docs = gx10._vault_docs(v.path)
        st = gx10.lifecycle_state(docs)
        assert "design" in st["present"]
        g = gx10.build_graph(v.slug, docs)
        ev = [n for n in g["nodes"].values() if n.get("stage") == "design"]
        assert ev
        assert any(n.get("type") == "evidence" for n in g["nodes"].values())
