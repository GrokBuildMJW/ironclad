"""#88 — C2 runnable target: the full skill lifecycle, end to end, model-free.

For BOTH kinds: generate from a spec (ack.skillgen) → pass the registration gate (ack.gate)
→ index + install via the library (ack.catalogue) → load into the engine
(gx10._load_plugins / _load_playbooks) → invoke (a tool handler returns a real result; a
playbook is loaded via use_skill). This is the R2 scenario for the epic's C2 completion gate.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=object))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402
import pytest  # noqa: E402

from ack import catalogue as cat  # noqa: E402
from ack import gate, skillgen  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    yield
    gx10._PLUGIN_TOOLS.clear()
    gx10._PLAYBOOKS.clear()


# A real, deterministic tool body — the "author/LLM fills the scaffold" step.
_FILLED_TOOL = (
    'CASE = {"name": "csv-summarize", "capability": "csv-summarize",\n'
    '        "description": "Summarize a CSV file", "type": "tool", "domain": "data",\n'
    '        "version": "0.1.0", "provenance": "user"}\n'
    "\n"
    "def run(path: str) -> str:\n"
    '    import csv\n'
    '    with open(path, newline="", encoding="utf-8") as f:\n'
    "        rows = list(csv.reader(f))\n"
    '    return f"{len(rows)} rows, {len(rows[0]) if rows else 0} columns"\n'
)


def test_e2e_typed_tool(tmp_path):
    lib, active = tmp_path / "lib", tmp_path / "active"
    # 1) generate (scaffold gives the structure + the auto-test), then fill the body
    skillgen.write_scaffold(skillgen.SkillSpec(
        capability="csv-summarize", description="Summarize a CSV file", kind="tool",
        domain="data", params=[("path", "str")]), lib)
    (lib / "skills" / "csv_summarize.py").write_text(_FILLED_TOOL, encoding="utf-8")

    # 2) gate (in the library, where the auto-test lives) must pass
    assert gate.gate_tool(lib / "skills" / "csv_summarize.py").passed

    # 3) library indexes it; install into the active skills dir
    c = cat.build_catalogue([(str(lib), "user")])
    assert c.get("csv-summarize").kind == "tool"
    cat.install(c.get("csv-summarize"), active)

    # 4) the engine loads it as an agent tool
    assert gx10._load_plugins(str(active)) == 1
    assert "csv-summarize" in {t["function"]["name"] for t in gx10._effective_tools()}

    # 5) invoke it (the runtime tool path) → a real result
    csv_file = tmp_path / "data.csv"
    csv_file.write_text("a,b,c\n1,2,3\n4,5,6\n", encoding="utf-8")
    handler = gx10._PLUGIN_TOOLS["csv-summarize"]["handler"]
    assert handler(path=str(csv_file)) == "3 rows, 3 columns"


def test_e2e_playbook(tmp_path):
    lib, active = tmp_path / "lib", tmp_path / "active"
    # 1) generate a playbook (its SKILL.md body is usable as-is for loading)
    skillgen.write_scaffold(skillgen.SkillSpec(
        capability="report-writing", description="Write a structured report", kind="playbook",
        type="capability", domain="writing", trigger=["write a report"]), lib)

    # 2) gate passes (frontmatter valid + references readable + scripts/check exits 0)
    assert gate.gate_playbook(lib / "skills" / "report-writing" / "SKILL.md").passed

    # 3) library indexes it; install into the active skills dir
    c = cat.build_catalogue([(str(lib), "user")])
    assert c.get("report-writing").kind == "playbook"
    cat.install(c.get("report-writing"), active)

    # 4) the engine loads it; use_skill is offered (progressive disclosure)
    assert gx10._load_playbooks(str(active)) == 1
    assert "use_skill" in {t["function"]["name"] for t in gx10._effective_tools()}

    # 5) invoke the runtime path: list → load body
    assert "report-writing" in gx10._use_skill("")
    body = gx10._use_skill("report-writing")
    assert "Write a structured report" in body


def test_e2e_unchecked_skill_is_rejected_by_the_gate(tmp_path):
    # A generated tool whose body was never filled (scaffold stub) still passes the structural
    # gate; but a tool with NO auto-test (unchecked code) is rejected — nothing unchecked
    # reaches the toolset.
    lib = tmp_path / "lib"
    skillgen.write_scaffold(skillgen.SkillSpec(
        capability="x-tool", description="d", kind="tool"), lib)
    (lib / "tests" / "test_x_tool.py").unlink()
    assert not gate.gate_tool(lib / "skills" / "x_tool.py").passed
