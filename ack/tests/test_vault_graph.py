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


def _seed(root):
    v = gx10.initiative_new("Graph Demo", "software")
    vdir = v.path
    (vdir / "decisions").mkdir(exist_ok=True)
    (vdir / "decisions" / "adr-0001.md").write_text(
        "---\ntitle: ADR One\ntype: decision\nstatus: accepted\n---\nbody\n",
        encoding="utf-8",
    )
    (vdir / "proposals").mkdir(exist_ok=True)
    (vdir / "proposals" / "spec-foo.md").write_text(
        "---\ntitle: Spec Foo\ndepends_on: [adr-0001]\nrefines: [does-not-exist]\n---\nbody\n",
        encoding="utf-8",
    )
    return v, vdir


def test_parse_edge_targets_bracket_and_csv():
    assert gx10._parse_edge_targets("[a, b]") == ["a", "b"]
    assert gx10._parse_edge_targets("a, b") == ["a", "b"]
    assert gx10._parse_edge_targets("") == []
    assert gx10._parse_edge_targets("decisions/adr-0001") == ["decisions/adr-0001"]


def test_parse_edge_targets_dedups_case_insensitive():
    assert gx10._parse_edge_targets("Foo, foo, bar") == ["Foo", "bar"]


def test_doc_edges_only_allowlisted_nonempty():
    fm = {"depends_on": "[a]", "relates_to": "", "unknown_edge": "[x]"}
    e = gx10._doc_edges(fm)
    assert e == {"depends_on": ["a"]}


def test_build_graph_nodes_keyed_by_relpath(tmp_path):
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        _seed(tmp_path)
        docs = gx10._vault_docs((Path(str(tmp_path)) / "vault" / "graph-demo"))
        g = gx10.build_graph("graph-demo", docs)
        assert set(g["nodes"]) == {"meta.md", "decisions/adr-0001.md", "proposals/spec-foo.md"}
        assert g["version"] == 1 and g["slug"] == "graph-demo"


def test_build_graph_resolves_and_flags_dangling(tmp_path):
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        _seed(tmp_path)
        docs = gx10._vault_docs((Path(str(tmp_path)) / "vault" / "graph-demo"))
        g = gx10.build_graph("graph-demo", docs)
        deps = [e for e in g["edges"] if e["type"] == "depends_on"]
        assert deps == [
            {
                "from": "proposals/spec-foo.md",
                "type": "depends_on",
                "to": "decisions/adr-0001.md",
                "resolved": True,
            }
        ]
        ref = [e for e in g["edges"] if e["type"] == "refines"][0]
        assert ref["resolved"] is False and ref["to"] == "does-not-exist"


def test_build_graph_deterministic(tmp_path):
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        _seed(tmp_path)
        docs = gx10._vault_docs((Path(str(tmp_path)) / "vault" / "graph-demo"))
        g1 = gx10.build_graph("graph-demo", docs)
        g2 = gx10.build_graph("graph-demo", docs)
        assert gx10._graph_json(g1) == gx10._graph_json(g2)


def test_graph_json_sorted_and_trailing_newline():
    s = gx10._graph_json(
        {
            "version": 1,
            "slug": "s",
            "generator": "reconcile_vault",
            "nodes": {},
            "edges": [],
        }
    )
    assert s.endswith("\n")
    assert json.loads(s)["version"] == 1


def test_reconcile_writes_graph_and_lifecycle(tmp_path):
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        v, vdir = _seed(tmp_path)
        gx10.reconcile_vault(v.slug, links=True)
        assert (vdir / "GRAPH.json").is_file()
        assert (vdir / "LIFECYCLE.md").is_file()
        g = json.loads((vdir / "GRAPH.json").read_text(encoding="utf-8"))
        assert "proposals/spec-foo.md" in g["nodes"]


def test_reconcile_graph_idempotent(tmp_path):
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        v, vdir = _seed(tmp_path)
        gx10.reconcile_vault(v.slug, links=True)
        a = (vdir / "GRAPH.json").read_text(encoding="utf-8")
        la = (vdir / "LIFECYCLE.md").read_text(encoding="utf-8")
        gx10.reconcile_vault(v.slug, links=True)
        assert (vdir / "GRAPH.json").read_text(encoding="utf-8") == a
        assert (vdir / "LIFECYCLE.md").read_text(encoding="utf-8") == la


def test_lifecycle_marks_dangling(tmp_path):
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        v, vdir = _seed(tmp_path)
        gx10.reconcile_vault(v.slug, links=True)
        life = (vdir / "LIFECYCLE.md").read_text(encoding="utf-8")
        assert "dangling" in life
        assert gx10._LIFECYCLE_AUTO_START in life and gx10._LIFECYCLE_AUTO_END in life


def test_generated_files_excluded_from_docs(tmp_path):
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        v, vdir = _seed(tmp_path)
        gx10.reconcile_vault(v.slug, links=True)
        names = [d["rel"].name for d in gx10._vault_docs(vdir)]
        assert "GRAPH.json" not in names and "LIFECYCLE.md" not in names


def test_build_graph_dedups_aliased_edges(tmp_path):
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        v = gx10.initiative_new("Alias Demo", "software")
        vdir = v.path
        (vdir / "decisions").mkdir(exist_ok=True)
        (vdir / "decisions" / "adr-0001.md").write_text("---\ntitle: A\n---\nx\n", encoding="utf-8")
        (vdir / "proposals").mkdir(exist_ok=True)
        (vdir / "proposals" / "s.md").write_text(
            "---\ntitle: S\ndepends_on: [adr-0001, decisions/adr-0001, decisions/adr-0001.md]\n---\nx\n",
            encoding="utf-8")
        docs = gx10._vault_docs(vdir)
        g = gx10.build_graph(v.slug, docs)
        deps = [e for e in g["edges"] if e["from"] == "proposals/s.md" and e["type"] == "depends_on"]
        assert deps == [{"from": "proposals/s.md", "type": "depends_on",
                         "to": "decisions/adr-0001.md", "resolved": True}]


def test_graph_json_is_key_order_independent():
    a = {"version": 1, "slug": "s", "generator": "g", "nodes": {"b.md": {}, "a.md": {}}, "edges": []}
    b = {"edges": [], "nodes": {"a.md": {}, "b.md": {}}, "slug": "s", "generator": "g", "version": 1}
    assert gx10._graph_json(a) == gx10._graph_json(b)   # sort_keys → identical regardless of insertion order


def test_frozen_markers_unchanged():
    # #1265: the INDEX marker is now English + description-less, consistent with its sibling markers.
    assert gx10._INDEX_AUTO_START == "<!-- ironclad:index:auto START -->"
    assert gx10._LINKS_AUTO_START == "<!-- ironclad:related:auto START -->"
    assert gx10._LIFECYCLE_AUTO_START == "<!-- ironclad:lifecycle:auto START -->"
