"""#1146/#1147 (epic #1144): the tool-call display shows a Claude-Code-style header (the command / target,
NOT the internal tool name) and the FULL result indented under a ``⎿`` corner — no ``execute_command(...)``
chrome, no 70-char preview cut, and long output is capped with an EXPLICIT ``… (+N more lines)`` marker.
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


def test_execute_command_shows_the_command_not_the_tool_name():
    # #1146: the operator never sees the internal `execute_command(command='…')` chrome.
    assert gx10._tool_display("execute_command", {"command": "cd /x && ls -1"}) == "Bash(cd /x && ls -1)"


def test_known_tools_map_to_human_labels():
    assert gx10._tool_display("read_file", {"path": "engine/gx10.py"}) == "Read(engine/gx10.py)"
    assert gx10._tool_display("create_issue", {"title": "fix the thing"}) == "Issue(fix the thing)"


def test_unknown_tool_falls_back_to_name_then_first_meaningful_arg():
    assert gx10._tool_display("advance_pipeline", {"task_id": "T-1"}) == "advance_pipeline(T-1)"
    assert gx10._tool_display("some_tool", {}) == "some_tool"


def test_result_is_shown_in_full_under_a_corner():
    out = gx10._tool_result_lines("AGENTS.md\nCLAUDE.md\nvessels/")
    assert out == ["  ⎿ AGENTS.md", "     CLAUDE.md", "     vessels/"]


def test_a_long_single_line_is_not_mid_line_truncated():
    # #1147: the old `preview[:70]` cut this to 70 chars; now the full line survives.
    long = "A" * 200
    assert gx10._tool_result_lines(long) == ["  ⎿ " + long]


def test_overlong_output_is_capped_with_an_explicit_more_marker():
    out = gx10._tool_result_lines("\n".join(f"row{i}" for i in range(70)), max_lines=60)
    assert sum(1 for line in out if "row" in line) == 60
    assert out[-1] == "     … (+10 more lines)"  # explicit, never a silent truncation
