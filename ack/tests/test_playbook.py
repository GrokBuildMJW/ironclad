"""Playbook skill kind (ADR-0001, #89): SKILL.md package format + loader + progressive
disclosure, and its engine exposure via the ``use_skill`` tool.

Deterministic, model/network-free: parse/validate/discover/lazy-load/route (ack.playbook)
plus ``gx10._load_playbooks`` / ``_use_skill`` (engine), mirroring test_plugins.py.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=object))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

from ack import playbook as pb  # noqa: E402


_SKILL_MD = """\
---
capability: report-writing
name: report-writing
description: Write a structured multi-section report
kind: playbook
type: capability
domain: writing
trigger: [write a report, tear sheet]
not_for: [single fact lookup]
version: 0.1.0
provenance: built-in
---

# Report writing

Stage 1: outline. Stage 2: draft. See references for the citation rules.
"""


def _make_playbook(root: Path, cap: str = "report-writing", *, body: str = _SKILL_MD,
                   refs: dict[str, str] | None = None) -> Path:
    d = root / "skills" / cap
    (d / "references").mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(body, encoding="utf-8")
    for name, text in (refs or {}).items():
        (d / "references" / name).write_text(text, encoding="utf-8")
    return d / "SKILL.md"


# ── frontmatter parsing ───────────────────────────────────────
def test_parse_frontmatter_scalars_and_lists():
    meta, body = pb.parse_frontmatter(_SKILL_MD)
    assert meta["capability"] == "report-writing"
    assert meta["version"] == "0.1.0" or meta["version"] == 0.1   # quoted/string preferred
    assert meta["trigger"] == ["write a report", "tear sheet"]
    assert meta["not_for"] == ["single fact lookup"]
    assert body.startswith("# Report writing")
    assert "Stage 1" in body


def test_parse_frontmatter_absent_returns_full_text():
    meta, body = pb.parse_frontmatter("# just markdown\n\nno frontmatter")
    assert meta == {} and body == "# just markdown\n\nno frontmatter"


def test_parse_frontmatter_unclosed_raises():
    with pytest.raises(pb.PlaybookError):
        pb.parse_frontmatter("---\ncapability: x\n# never closed\n")


# ── schema validation ─────────────────────────────────────────
def test_validate_meta_requires_core_fields():
    errs = pb.validate_meta({"kind": "playbook"})
    assert any("capability" in e for e in errs) and any("description" in e for e in errs)


def test_validate_meta_rejects_wrong_kind_and_listfield():
    errs = pb.validate_meta({"capability": "x", "description": "y", "kind": "tool",
                             "trigger": "not-a-list"})
    assert any("kind must be" in e for e in errs)
    assert any("trigger" in e and "list" in e for e in errs)


# ── Playbook object: eager meta, lazy body/references ──────────
def test_parse_playbook_and_metadata_is_disclosure_first(tmp_path):
    p = pb.parse_playbook(_make_playbook(tmp_path))
    md = p.metadata()
    assert md["capability"] == "report-writing" and md["kind"] == "playbook"
    assert md["domain"] == "writing" and md["trigger"] == ["write a report", "tear sheet"]
    # metadata view carries no body
    assert "body" not in md


def test_body_is_lazy_and_correct(tmp_path):
    p = pb.parse_playbook(_make_playbook(tmp_path))
    assert p._body_cache is None          # not materialized until accessed
    assert "Stage 1: outline" in p.body
    assert p._body_cache is not None      # cached after access


def test_references_list_without_reading_and_read_on_demand(tmp_path):
    p = pb.parse_playbook(_make_playbook(tmp_path, refs={"citation.md": "Cite APA."}))
    assert p.references() == ["citation.md"]          # listed, not read
    assert p.reference("citation.md") == "Cite APA."  # read on demand


def test_reference_missing_and_traversal_blocked(tmp_path):
    p = pb.parse_playbook(_make_playbook(tmp_path, refs={"a.md": "A"}))
    with pytest.raises(pb.PlaybookError):
        p.reference("nope.md")
    with pytest.raises(pb.PlaybookError):
        p.reference("../../SKILL.md")   # path traversal collapses to a basename → absent


def test_reference_symlink_escape_blocked(tmp_path):
    # M20 (#1487): a planted references/*.md symlink to a host file OUTSIDE the playbook must not be
    # readable through use_skill — Path(name).name blocks textual ../ but read_text follows symlinks.
    secret = tmp_path / "host_secret.txt"
    secret.write_text("TOP SECRET", encoding="utf-8")
    skill_md = _make_playbook(tmp_path, refs={"real.md": "R"})
    link = skill_md.parent / "references" / "leak.md"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this platform")
    p = pb.parse_playbook(skill_md)
    assert p.references() == ["real.md"]              # the symlink is NOT listed
    with pytest.raises(pb.PlaybookError):
        p.reference("leak.md")                        # and NOT readable (containment holds)
    assert p.reference("real.md") == "R"              # the real reference still works


def test_reference_dir_symlink_escape_blocked(tmp_path):
    # M20 (#1487): if references/ ITSELF is a symlink/junction to a host dir, a real file under it must
    # NOT read as contained — containment anchors to the resolved playbook dir, not the relocatable
    # references/ dir (a resolve()-based anchor would follow the reparse point and leak the host dir).
    host = tmp_path / "host_dir"
    host.mkdir()
    (host / "passwd.md").write_text("HOST SECRET", encoding="utf-8")
    d = tmp_path / "skills" / "cap"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(_SKILL_MD, encoding="utf-8")
    try:
        (d / "references").symlink_to(host, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlink creation not permitted on this platform")
    p = pb.parse_playbook(d / "SKILL.md")
    assert p.references() == []                       # a symlinked references/ dir lists nothing
    with pytest.raises(pb.PlaybookError):
        p.reference("passwd.md")                      # and reads nothing (containment holds)


def test_trigger_routing(tmp_path):
    p = pb.parse_playbook(_make_playbook(tmp_path))
    assert p.matches("please write a report on X")
    assert not p.matches("what is the capital of France")


# ── discovery (fail-soft, dedup) ──────────────────────────────
def test_discover_finds_valid_skips_broken_and_dedups(tmp_path):
    _make_playbook(tmp_path, "report-writing")
    _make_playbook(tmp_path, "copywriting",
                   body=_SKILL_MD.replace("report-writing", "copywriting"))
    # a broken package (frontmatter missing required capability) is skipped, not fatal
    bad = tmp_path / "skills" / "broken"
    bad.mkdir(parents=True, exist_ok=True)
    (bad / "SKILL.md").write_text("---\nkind: playbook\ndescription: no capability\n---\nx\n",
                                  encoding="utf-8")
    found = {p.capability for p in pb.discover_playbooks(tmp_path)}
    assert found == {"report-writing", "copywriting"}


def test_discover_missing_root_is_empty():
    assert pb.discover_playbooks("/no/such/dir") == []


# ── engine exposure: _load_playbooks + use_skill ──────────────
@pytest.fixture()
def gx10_mod():
    import gx10
    yield gx10
    gx10._PLAYBOOKS.clear()


def test_engine_loads_playbooks_and_offers_use_skill(tmp_path, gx10_mod):
    _make_playbook(tmp_path, "report-writing", refs={"citation.md": "Cite APA."})
    assert gx10_mod._load_playbooks(str(tmp_path)) == 1
    assert "report-writing" in gx10_mod._PLAYBOOKS
    names = [t["function"]["name"] for t in gx10_mod._effective_tools()]
    assert "use_skill" in names


def test_use_skill_progressive_disclosure(tmp_path, gx10_mod):
    _make_playbook(tmp_path, "report-writing", refs={"citation.md": "Cite APA."})
    gx10_mod._load_playbooks(str(tmp_path))
    listing = gx10_mod._use_skill("")
    assert "report-writing" in listing and "load one" in listing
    body = gx10_mod._use_skill("report-writing")
    assert "Stage 1: outline" in body and "citation.md" in body   # body + refs hint
    ref = gx10_mod._use_skill("report-writing", "citation.md")
    assert ref == "Cite APA."
    assert "ERROR" in gx10_mod._use_skill("does-not-exist")


def test_no_playbooks_no_tool(tmp_path, gx10_mod):
    gx10_mod._PLAYBOOKS.clear()
    names = [t["function"]["name"] for t in gx10_mod._effective_tools()]
    assert "use_skill" not in names
