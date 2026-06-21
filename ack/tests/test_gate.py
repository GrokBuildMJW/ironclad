"""Registration quality gate (#34): no unchecked skill registers.

tool = doctor preflight (loadable, CASE+capability, sync run, derivable schema) + a test
ships; playbook = frontmatter valid + references readable + scripts/check passes.
Fixtures are authored with ack.skillgen (dogfooding).
"""
from __future__ import annotations

from pathlib import Path

from ack import gate
from ack import skillgen


def _tool(root: Path, cap="csv-summarize"):
    skillgen.write_scaffold(skillgen.SkillSpec(
        capability=cap, description="Summarize a CSV", kind="tool",
        params=[("path", "str")]), root, force=True)
    return root / "skills" / f"{cap.replace('-', '_')}.py"


def _playbook(root: Path, cap="report-writing"):
    skillgen.write_scaffold(skillgen.SkillSpec(
        capability=cap, description="Write a report", kind="playbook",
        type="capability", domain="writing", trigger=["write a report"]), root, force=True)
    return root / "skills" / cap / "SKILL.md"


# ── tool gate ─────────────────────────────────────────────────
def test_gate_tool_passes_on_scaffold(tmp_path):
    res = gate.gate_tool(_tool(tmp_path))
    assert res.passed and res.kind == "tool", res.reasons


def test_gate_tool_fails_without_test(tmp_path):
    py = _tool(tmp_path)
    (tmp_path / "tests" / "test_csv_summarize.py").unlink()
    res = gate.gate_tool(py)
    assert not res.passed and any("test" in r for r in res.reasons)


def test_gate_tool_fails_on_missing_case_and_async(tmp_path):
    skills = tmp_path / "skills"; skills.mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_bad.py").write_text("def test_x(): pass\n", encoding="utf-8")
    (skills / "bad.py").write_text("async def run(x: str) -> str:\n    return x\n", encoding="utf-8")
    res = gate.gate_tool(skills / "bad.py")
    assert not res.passed
    assert any("CASE" in r for r in res.reasons)
    assert any("synchronous" in r for r in res.reasons)


# ── playbook gate ─────────────────────────────────────────────
def test_gate_playbook_passes_on_scaffold(tmp_path):
    res = gate.gate_playbook(_playbook(tmp_path))
    assert res.passed and res.kind == "playbook", res.reasons


def test_gate_playbook_fails_on_bad_frontmatter(tmp_path):
    md = _playbook(tmp_path)
    md.write_text("---\nkind: playbook\ndescription: no capability\n---\nbody\n", encoding="utf-8")
    res = gate.gate_playbook(md)
    assert not res.passed and any("capability" in r or "invalid" in r for r in res.reasons)


def test_gate_playbook_scripts_check_runs(tmp_path):
    # the scaffolded self-contained scripts/check must exit 0 for a valid SKILL.md
    md = _playbook(tmp_path)
    assert (md.parent / "scripts" / "check").is_file()
    assert gate.gate_playbook(md, run_check=True).passed


# ── prompt gate (#111) ────────────────────────────────────────
def _prompt(root: Path, cap="blog-post", *, de=True, required_unused=False) -> Path:
    d = root / "skills" / cap
    (d / "locales").mkdir(parents=True, exist_ok=True)
    # 'extra' is declared AND required but never used in the template → a defect the gate catches.
    variables = "[topic, audience, extra]" if required_unused else "[topic, audience]"
    req = "[topic, extra]" if required_unused else "[topic]"
    (d / "SKILL.md").write_text(
        "---\n"
        f"capability: {cap}\nkind: prompt\ndescription: Draft a brief\n"
        f"languages: [en, de]\nvariables: {variables}\n"
        f"required: {req}\n---\n"
        "Write a {audience}-facing post about {topic}.\n",
        encoding="utf-8")
    if de:
        (d / "locales" / "de.json").write_text(
            '{"template": "Schreibe fuer {audience} ueber {topic}."}', encoding="utf-8")
    return d / "SKILL.md"


def test_gate_prompt_passes_on_valid_item(tmp_path):
    res = gate.gate_prompt(_prompt(tmp_path))
    assert res.passed and res.kind == "prompt", res.reasons


def test_gate_prompt_passes_without_overlay_via_fallback(tmp_path):
    res = gate.gate_prompt(_prompt(tmp_path, de=False))   # no de.json → source fallback
    assert res.passed, res.reasons


def test_gate_prompt_fails_when_required_var_unused(tmp_path):
    res = gate.gate_prompt(_prompt(tmp_path, required_unused=True))
    assert not res.passed and any("extra" in r and "never used" in r for r in res.reasons)


def test_gate_prompt_fails_on_bad_frontmatter(tmp_path):
    md = _prompt(tmp_path)
    md.write_text("---\nkind: prompt\ndescription: no capability\n---\nbody {x}\n", encoding="utf-8")
    res = gate.gate_prompt(md)
    assert not res.passed and any("capability" in r or "invalid" in r for r in res.reasons)


def test_gate_prompt_fails_on_broken_overlay_json(tmp_path):
    md = _prompt(tmp_path)
    (md.parent / "locales" / "de.json").write_text("{not json", encoding="utf-8")
    res = gate.gate_prompt(md)
    assert not res.passed and any("'de'" in r for r in res.reasons)


# ── dispatcher ────────────────────────────────────────────────
def test_gate_dispatch_by_path(tmp_path):
    assert gate.gate(_tool(tmp_path, "t1")).kind == "tool"
    assert gate.gate(_playbook(tmp_path, "pb1")).kind == "playbook"
    assert gate.gate(_playbook(tmp_path, "pb2").parent).kind == "playbook"  # dir form
    assert gate.gate(_prompt(tmp_path, "pr1")).kind == "prompt"             # kind: prompt SKILL.md
    assert gate.gate(_prompt(tmp_path, "pr2").parent).kind == "prompt"      # dir form
