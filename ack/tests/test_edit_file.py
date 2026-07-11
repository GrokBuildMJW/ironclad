"""#1075 (epic #1043 quick-win): the targeted edit_file (str_replace) tool.

A small edit no longer rewrites the whole file (token-costly + risky) — edit_file replaces an EXACT string,
required unique unless replace_all, atomic write. Mirrors the Edit-tool contract.
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


def test_edit_file_is_registered():
    assert "edit_file" in {t["function"]["name"] for t in gx10.TOOLS}


def test_unique_replace(tmp_path):
    f = tmp_path / "x.py"
    f.write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")
    out = gx10.run_tool("edit_file", {"path": str(f), "old_string": "b = 2", "new_string": "b = 20"})
    assert out.startswith("OK: edited")
    assert f.read_text(encoding="utf-8") == "a = 1\nb = 20\nc = 3\n"


def test_old_string_not_found(tmp_path):
    f = tmp_path / "x.py"
    f.write_text("hello\n", encoding="utf-8")
    out = gx10.run_tool("edit_file", {"path": str(f), "old_string": "nope", "new_string": "y"})
    assert out.startswith("ERROR: old_string not found")
    assert f.read_text(encoding="utf-8") == "hello\n"                # unchanged


def test_noop_edit_is_error_not_false_success(tmp_path):
    # #1317: a no-op edit (old_string == new_string, or already applied) must NOT report success — it
    # surfaces as ERROR, so "OK: edited" always means the bytes actually changed (the false-success half
    # of the orchestrator-bridge bug).
    f = tmp_path / "x.py"
    f.write_text("a = 1\n", encoding="utf-8")
    out = gx10.run_tool("edit_file", {"path": str(f), "old_string": "a = 1", "new_string": "a = 1"})
    assert out.startswith("ERROR: no change")
    assert f.read_text(encoding="utf-8") == "a = 1\n"                # byte-identical, nothing written


def test_ambiguous_without_replace_all_is_refused(tmp_path):
    f = tmp_path / "x.py"
    f.write_text("x\nx\n", encoding="utf-8")
    out = gx10.run_tool("edit_file", {"path": str(f), "old_string": "x", "new_string": "y"})
    assert "not unique" in out and "2 occurrences" in out
    assert f.read_text(encoding="utf-8") == "x\nx\n"                 # refused → unchanged (safe)


def test_replace_all(tmp_path):
    f = tmp_path / "x.py"
    f.write_text("x\nx\nx\n", encoding="utf-8")
    out = gx10.run_tool("edit_file", {"path": str(f), "old_string": "x", "new_string": "y", "replace_all": True})
    assert "3 replacement" in out
    assert f.read_text(encoding="utf-8") == "y\ny\ny\n"


def test_missing_file_and_empty_old_string(tmp_path):
    out = gx10.run_tool("edit_file", {"path": str(tmp_path / "nope.py"), "old_string": "a", "new_string": "b"})
    assert out.startswith("ERROR: Not found")
    f = tmp_path / "y.py"
    f.write_text("data\n", encoding="utf-8")
    out2 = gx10.run_tool("edit_file", {"path": str(f), "old_string": "", "new_string": "z"})
    assert out2.startswith("ERROR: edit_file needs a non-empty old_string")
