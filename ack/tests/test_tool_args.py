"""Validate→Reask at the tool boundary (gx10._parse_tool_args / _validate_tool_args).

P-1/P-2: malformed JSON or a schema violation in a tool call must come back as an error
the model can act on — NOT be silently degraded to empty args. Lightweight top-level
schema check (required + types), enough to drive a reask.
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

_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "limit": {"type": "integer"},
        "ratio": {"type": "number"},
        "items": {"type": "array"},
    },
    "required": ["path"],
}


# ── _valid_tool_args_json — history must stay renderable (no vLLM 400 on reask) ──
def test_valid_tool_args_json_keeps_valid_and_repairs_malformed():
    import json
    good = '{"path": "epic.md", "content": "line1\\nline2"}'
    assert gx10._valid_tool_args_json(good) == good          # valid JSON preserved verbatim
    # a malformed arguments string (a small model's huge unescaped write_file content) → safe placeholder,
    # so vLLM's tool-call rendering json.loads() on the NEXT request cannot 400 and defeat Validate→Reask.
    for bad in ['{"path": "a", "content": "oops',           # unterminated string
                '{"path": "a" "content": "b"}',             # missing comma (the operator's 400)
                '{bad json', 'not json at all', '{"a":}', None, ""]:
        out = gx10._valid_tool_args_json(bad)
        assert out == "{}"
        json.loads(out)                                      # the stored arguments always render


def test_required_missing():
    assert "missing required" in gx10._validate_tool_args({"limit": 1}, _SCHEMA)


def test_type_mismatch():
    err = gx10._validate_tool_args({"path": 123}, _SCHEMA)
    assert err and "must be string" in err


def test_bool_rejected_for_integer():
    # bool is a subclass of int → must still be rejected where an integer is expected.
    err = gx10._validate_tool_args({"path": "x", "limit": True}, _SCHEMA)
    assert err and "boolean" in err


def test_number_accepts_int_and_float():
    assert gx10._validate_tool_args({"path": "x", "ratio": 3}, _SCHEMA) is None
    assert gx10._validate_tool_args({"path": "x", "ratio": 3.5}, _SCHEMA) is None


def test_valid_passes():
    assert gx10._validate_tool_args(
        {"path": "x", "limit": 5, "items": [1, 2]}, _SCHEMA) is None


def test_no_schema_is_noop():
    assert gx10._validate_tool_args({"anything": 1}, None) is None
    assert gx10._validate_tool_args({"anything": 1}, {}) is None


# ── _parse_tool_args (parse + schema, with a controlled tool) ─
@pytest.fixture
def _fake_tool(monkeypatch):
    tool = {"type": "function", "function": {"name": "demo", "parameters": _SCHEMA}}
    monkeypatch.setattr(gx10, "_effective_tools", lambda: [tool])


def test_parse_malformed_json_reasks(_fake_tool):
    args, err = gx10._parse_tool_args("demo", '{"path": "x"')   # truncated
    assert args is None and "malformed JSON" in err


def test_parse_non_dict_reasks(_fake_tool):
    args, err = gx10._parse_tool_args("demo", "[1, 2, 3]")
    assert args is None and "must be a JSON object" in err


def test_parse_schema_violation_reasks(_fake_tool):
    args, err = gx10._parse_tool_args("demo", '{"limit": 5}')   # missing required path
    assert args is None and "invalid arguments" in err and "missing required" in err


def test_parse_valid_returns_args(_fake_tool):
    args, err = gx10._parse_tool_args("demo", '{"path": "src/x.py", "limit": 3}')
    assert err is None and args == {"path": "src/x.py", "limit": 3}


def test_parse_empty_is_empty_dict(_fake_tool):
    # No args at all → empty dict (schema with required will then flag it, but a
    # schema-less/required-less tool accepts it). Here 'demo' requires path → error.
    args, err = gx10._parse_tool_args("demo", "")
    assert args is None and "missing required" in err


def test_parse_unknown_tool_skips_schema(monkeypatch):
    monkeypatch.setattr(gx10, "_effective_tools", lambda: [])   # no schema for any name
    args, err = gx10._parse_tool_args("whatever", '{"x": 1}')
    assert err is None and args == {"x": 1}                      # JSON ok, no schema gate
