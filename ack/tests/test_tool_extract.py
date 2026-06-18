"""Model-agnostic tool-call recovery (gx10._extract_tool_calls_from_text).

P-3: when an OpenAI-compatible endpoint returns no native ``tool_calls``, recover any the
model emitted as text — <tool_call> tags, fenced json, or a bare object — but ONLY when the
named tool is known, so a legitimate JSON answer is never hijacked.
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

_TOOLS = {"read_file", "write_file"}


def test_tool_call_tag():
    txt = 'Sure.\n<tool_call>{"name": "read_file", "arguments": {"path": "a.py"}}</tool_call>'
    out = gx10._extract_tool_calls_from_text(txt, _TOOLS)
    assert len(out) == 1
    assert out[0]["name"] == "read_file"
    assert json.loads(out[0]["arguments"]) == {"path": "a.py"}


def test_multiple_tool_call_tags():
    txt = ('<tool_call>{"name":"read_file","arguments":{"path":"a"}}</tool_call>'
           '<tool_call>{"name":"write_file","arguments":{"path":"b","content":"x"}}</tool_call>')
    out = gx10._extract_tool_calls_from_text(txt, _TOOLS)
    assert [c["name"] for c in out] == ["read_file", "write_file"]


def test_fenced_json():
    txt = 'Let me read it:\n```json\n{"name": "read_file", "arguments": {"path": "a.py"}}\n```'
    out = gx10._extract_tool_calls_from_text(txt, _TOOLS)
    assert len(out) == 1 and out[0]["name"] == "read_file"


def test_bare_object_is_NOT_recovered():
    # SECURITY: a bare top-level JSON object must NOT be treated as a call, even if its
    # name matches a tool — otherwise a legitimate JSON answer (or an echoed tool spec)
    # could be hijacked into a destructive call. Only explicit <tool_call>/fence markers.
    txt = '{"name": "read_file", "arguments": {"path": "a.py"}}'
    assert gx10._extract_tool_calls_from_text(txt, _TOOLS) == []


def test_bare_destructive_object_not_hijacked():
    txt = '{"name": "execute_command", "arguments": {"command": "rm -rf /"}}'
    assert gx10._extract_tool_calls_from_text(txt, _TOOLS | {"execute_command"}) == []


def test_parameters_alias_and_dict_serialised():
    txt = '<tool_call>{"name": "read_file", "parameters": {"path": "a.py"}}</tool_call>'
    out = gx10._extract_tool_calls_from_text(txt, _TOOLS)
    assert json.loads(out[0]["arguments"]) == {"path": "a.py"}


def test_function_shape():
    txt = '<tool_call>{"function": {"name": "read_file", "arguments": {"path": "a"}}}</tool_call>'
    out = gx10._extract_tool_calls_from_text(txt, _TOOLS)
    assert out[0]["name"] == "read_file"


def test_unknown_tool_not_hijacked():
    # A JSON object that is NOT a known tool must be ignored (it's just data).
    txt = '{"name": "some_user_record", "arguments": {"id": 5}}'
    assert gx10._extract_tool_calls_from_text(txt, _TOOLS) == []


def test_plain_prose_ignored():
    assert gx10._extract_tool_calls_from_text("Here is the answer: 42.", _TOOLS) == []


def test_missing_arguments_default_empty():
    txt = '<tool_call>{"name": "read_file"}</tool_call>'
    out = gx10._extract_tool_calls_from_text(txt, _TOOLS)
    assert out[0]["arguments"] == "{}"


def test_malformed_json_in_tag_skipped():
    txt = '<tool_call>{"name": "read_file", "arguments": {</tool_call>'
    assert gx10._extract_tool_calls_from_text(txt, _TOOLS) == []


def test_empty_inputs():
    assert gx10._extract_tool_calls_from_text("", _TOOLS) == []
    assert gx10._extract_tool_calls_from_text('{"name":"read_file"}', set()) == []
