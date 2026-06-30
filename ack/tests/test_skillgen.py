"""Skill generator (#33): spec → schema-valid scaffold for both kinds (tool + playbook).

Deterministic: the scaffold is contract-correct by construction — a generated tool is
discoverable with a well-formed schema, a generated playbook parses + validates + discovers.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ack import playbook as pb
from ack import skillgen
from ack.registry import Registry, derive_tool_schema


# ── spec validation ───────────────────────────────────────────
def test_spec_requires_capability_and_description():
    with pytest.raises(ValueError):
        skillgen.SkillSpec(capability="", description="x")
    with pytest.raises(ValueError):
        skillgen.SkillSpec(capability="x", description="")


def test_spec_rejects_bad_kind_and_param():
    with pytest.raises(ValueError):
        skillgen.SkillSpec(capability="x", description="d", kind="weird")
    with pytest.raises(ValueError):
        skillgen.SkillSpec(capability="x", description="d", params=[("n", "complex")])


def test_capability_is_slugified():
    s = skillgen.SkillSpec(capability="CSV Summarize!", description="d")
    assert s.capability == "csv-summarize"


# ── tool scaffold: discoverable + well-formed schema ──────────
def test_tool_scaffold_is_discoverable_with_schema(tmp_path):
    spec = skillgen.SkillSpec(capability="csv-summarize", description="Summarize a CSV file",
                              kind="tool", domain="data", params=[("path", "str"), ("rows", "int")])
    written = skillgen.write_scaffold(spec, tmp_path)
    assert any(p.name == "csv_summarize.py" for p in written)
    regs = {r.capability: r for r in Registry().discover_skills(str(tmp_path))}
    assert "csv-summarize" in regs
    schema = derive_tool_schema(regs["csv-summarize"].handler)
    assert schema["type"] == "object"
    props = schema["properties"]
    assert props["path"]["type"] == "string" and props["rows"]["type"] == "integer"
    # an auto-test stub is generated alongside
    assert (tmp_path / "tests" / "test_csv_summarize.py").is_file()


# ── playbook scaffold: parses + validates + discovers ─────────
def test_playbook_scaffold_parses_and_discovers(tmp_path):
    spec = skillgen.SkillSpec(capability="report-writing", description="Write a report",
                              kind="playbook", type="capability", domain="writing",
                              trigger=["write a report"])
    skillgen.write_scaffold(spec, tmp_path)
    skill_md = tmp_path / "skills" / "report-writing" / "SKILL.md"
    assert skill_md.is_file()
    parsed = pb.parse_playbook(skill_md)          # frontmatter valid by construction
    assert parsed.capability == "report-writing"
    assert parsed.meta["trigger"] == ["write a report"]
    assert pb.validate_meta(parsed.meta) == []
    found = {p.capability for p in pb.discover_playbooks(tmp_path)}
    assert "report-writing" in found
    # the file-first validation gate script is scaffolded
    assert (tmp_path / "skills" / "report-writing" / "scripts" / "check").is_file()


# ── overwrite protection ──────────────────────────────────────
def test_write_refuses_overwrite_then_force(tmp_path):
    spec = skillgen.SkillSpec(capability="dup", description="d", kind="tool")
    skillgen.write_scaffold(spec, tmp_path)
    with pytest.raises(FileExistsError):
        skillgen.write_scaffold(spec, tmp_path)
    # force overwrites cleanly
    assert skillgen.write_scaffold(spec, tmp_path, force=True)


def test_cli_writes_a_playbook(tmp_path):
    rc = skillgen.main(["--capability", "demo-pb", "--description", "demo",
                        "--kind", "playbook", "--trigger", "do demo", "--dest", str(tmp_path)])
    assert rc == 0
    assert (tmp_path / "skills" / "demo-pb" / "SKILL.md").is_file()


# ── GEN-1 (#503): free-text with quotes/backslashes/newlines stays valid by construction ──
def test_tool_scaffold_with_hostile_freetext_is_importable():
    desc = 'Wrap text in "quotes", a back\\slash and\na newline'
    spec = skillgen.SkillSpec(capability="wrap-text", description=desc, kind="tool", domain='da"ta')
    src = next(v for k, v in skillgen.render_tool(spec).items() if k.startswith("skills/"))
    ns: dict = {}
    exec(compile(src, "<generated>", "exec"), ns)     # pre-fix: SyntaxError (unescaped quotes/backslash)
    assert ns["CASE"]["description"] == desc           # free text round-trips intact through the CASE literal
    assert ns["CASE"]["domain"] == 'da"ta'


def test_playbook_frontmatter_survives_hostile_description(tmp_path):
    desc = 'Summarize: handle "quotes", a back\\slash\nand a second line'
    spec = skillgen.SkillSpec(capability="summarize-x", description=desc, kind="playbook")
    md = next(v for k, v in skillgen.render_playbook(spec).items() if k.endswith("SKILL.md"))
    p = tmp_path / "SKILL.md"
    p.write_text(md, encoding="utf-8")
    parsed = pb.parse_playbook(p)                       # pre-fix: PlaybookError (the newline spilled a line)
    assert pb.validate_meta(parsed.meta) == []          # frontmatter valid
    assert parsed.meta["description"] == " ".join(desc.split())   # flattened to a single YAML line, intact
