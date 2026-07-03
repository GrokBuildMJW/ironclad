"""L1-write (#1048, epic #1043): escape-free authoring for a small model.

`write_file` gains `mode='append'` (build a large file in chunks) and a new model-callable
`write_last_reply(path)` persists the model's PREVIOUS reply text (produced as ordinary streamed output)
instead of a huge JSON-escaped `content` argument the model mis-escapes. A warn-only integrity guard flags a
write whose emitting generation was cut off by the token limit (`finish_reason=length`).
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


def test_write_file_mode_write_and_append(tmp_path):
    f = tmp_path / "out.md"
    r1 = gx10.run_tool("write_file", {"path": str(f), "content": "line1\n"})
    assert "Written" in r1 and f.read_text(encoding="utf-8") == "line1\n"
    r2 = gx10.run_tool("write_file", {"path": str(f), "content": "line2\n", "mode": "append"})
    assert "Appended" in r2 and f.read_text(encoding="utf-8") == "line1\nline2\n"
    # default mode replaces (atomic), not appends
    gx10.run_tool("write_file", {"path": str(f), "content": "fresh\n"})
    assert f.read_text(encoding="utf-8") == "fresh\n"


def test_write_last_reply_and_write_file_mode_are_registered():
    tools = {t["function"]["name"] for t in gx10.TOOLS}
    assert "write_last_reply" in tools
    wf = next(t for t in gx10.TOOLS if t["function"]["name"] == "write_file")
    assert "mode" in wf["function"]["parameters"]["properties"]                 # append param exposed
    assert wf["function"]["parameters"]["properties"]["mode"]["enum"] == ["write", "append"]
    wlr = next(t for t in gx10.TOOLS if t["function"]["name"] == "write_last_reply")
    assert wlr["function"]["parameters"]["required"] == ["path"]                # content comes from last reply
    assert "content" not in wlr["function"]["parameters"]["properties"]         # escape-free: no content arg
