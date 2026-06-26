"""Epic #505, S12 — web_search robustness (operator-test finding).

A: `execute_command` refuses a known TOOL name typed as a shell command (the model sometimes runs
   `web_search "…"` via the shell when the tool is not offered) — a clear redirect, not a shell error.
B: the EN+DE current-info classifier flags news/headline markers ("aktuelle meldungen …") so the
   proactive web_search steer fires.
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


# ── A: known tool name typed as a shell command ─────────────────────────────
def test_all_tool_names_includes_known_tools():
    names = gx10._all_tool_names()
    for n in ("web_search", "read_file", "execute_command", "query_memory"):
        assert n in names


def test_execute_command_blocks_web_search_used_as_shell():
    out = gx10.run_tool("execute_command", {"command": 'web_search "Iran latest news"'})
    assert out.startswith("BLOCKED") and "is a tool" in out and "web_search" in out
    assert "search.adapter" in out and "GX10_SEARCH_API_KEY" in out   # the web_search-specific hint


def test_execute_command_blocks_other_tool_used_as_shell():
    out = gx10.run_tool("execute_command", {"command": "query_memory foo bar"})
    assert out.startswith("BLOCKED") and "is a tool" in out and "query_memory" in out
    assert "search.adapter" not in out                                # only web_search gets that hint


def test_execute_command_allows_a_normal_command(monkeypatch):
    monkeypatch.setattr(gx10, "PLATFORM", "windows")
    monkeypatch.setattr(gx10.subprocess, "run",
                        lambda argv, **kw: types.SimpleNamespace(stdout="ok", stderr="", returncode=0))
    out = gx10.run_tool("execute_command", {"command": "Get-Date"})
    assert "is a tool" not in out                                     # not a tool name → runs normally


def test_tool_guard_strips_quotes_and_case():
    out = gx10.run_tool("execute_command", {"command": "  Web_Search 'x y'"})
    assert out.startswith("BLOCKED") and "is a tool" in out           # case-insensitive, quote-stripped


# ── B: current-info classifier covers news/headline markers ─────────────────
@pytest.mark.parametrize("q", [
    "aktuelle meldungen zum irankrieg", "schlagzeilen heute", "latest headlines on X",
])
def test_classifier_flags_news_markers(q):
    assert gx10._is_current_info_query(q) is True


@pytest.mark.parametrize("q", [
    "refactor the current function", "read the meldungen module",  # 'meldungen' alone must NOT match
])
def test_classifier_ignores_non_recency(q):
    assert gx10._is_current_info_query(q) is False
