"""Config-tree loader (gx10._load_config_tree) — directory descent, index, includes.

Regression for the subdir bug: a config directory is a TREE, so nested files like
``conf/connection/connection.json`` must load — the old top-level-only glob dropped them.
Also pins the designed behaviours: a ``gx10.config.json`` index wins over loose files, and
file-level ``include`` lists merge with inline blocks taking precedence.
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=object))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402


def _w(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj), encoding="utf-8")


def test_descends_into_subdirectories(tmp_path):
    # The bug: connection.json in a subdir was never loaded.
    _w(tmp_path / "connection" / "connection.json",
       {"connection": {"base_url": "http://host:8000/v1", "model": "m"}})
    cfg = gx10._load_config_tree(tmp_path)
    assert cfg["connection"]["base_url"] == "http://host:8000/v1"
    assert cfg["connection"]["model"] == "m"


def test_toplevel_and_subdir_merge(tmp_path):
    _w(tmp_path / "a.json", {"generation": {"max_tokens": 100}})
    _w(tmp_path / "sub" / "b.json", {"generation": {"temperature": 0.5}})
    cfg = gx10._load_config_tree(tmp_path)
    # both blocks present, field-wise deep-merge across the tree
    assert cfg["generation"] == {"max_tokens": 100, "temperature": 0.5}


def test_subdir_overrides_toplevel(tmp_path):
    _w(tmp_path / "a.json", {"connection": {"model": "base"}})
    _w(tmp_path / "z_sub" / "b.json", {"connection": {"model": "override"}})
    cfg = gx10._load_config_tree(tmp_path)
    assert cfg["connection"]["model"] == "override"   # subdirs win over top-level


def test_hidden_subdirs_are_skipped(tmp_path):
    # A .git / .vscode style dir with json must NOT be slurped into the config.
    _w(tmp_path / "a.json", {"connection": {"model": "real"}})
    _w(tmp_path / ".git" / "junk.json", {"connection": {"model": "HIJACKED"}})
    cfg = gx10._load_config_tree(tmp_path)
    assert cfg["connection"]["model"] == "real"


def test_index_file_wins_over_loose_files(tmp_path):
    _w(tmp_path / "gx10.config.json", {"connection": {"model": "from-index"}})
    _w(tmp_path / "other.json", {"connection": {"model": "loose"}})
    _w(tmp_path / "sub" / "s.json", {"connection": {"model": "subdir"}})
    cfg = gx10._load_config_tree(tmp_path)
    assert cfg["connection"]["model"] == "from-index"  # index short-circuits the tree


def test_include_merges_with_inline_precedence(tmp_path):
    _w(tmp_path / "base.json", {"connection": {"model": "base", "api_key_env": "K"}})
    _w(tmp_path / "main.json",
       {"include": ["base.json"], "connection": {"model": "inline"}})
    cfg = gx10._load_config_tree(tmp_path / "main.json")
    assert cfg["connection"]["model"] == "inline"      # inline overrides the include
    assert cfg["connection"]["api_key_env"] == "K"     # …but keeps non-overridden fields


def test_comment_keys_stripped(tmp_path):
    _w(tmp_path / "c.json", {"_comment": "private note", "connection": {"model": "m"}})
    cfg = gx10._load_config_tree(tmp_path)
    assert "_comment" not in cfg and cfg["connection"]["model"] == "m"


def test_cycle_guard_via_include(tmp_path):
    # main includes itself → cycle guard must prevent infinite recursion.
    _w(tmp_path / "main.json", {"include": ["main.json"], "connection": {"model": "m"}})
    cfg = gx10._load_config_tree(tmp_path / "main.json")
    assert cfg["connection"]["model"] == "m"
