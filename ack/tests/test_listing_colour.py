"""#1196 (epic #1144): coloured directory listings. `ls -lA --color=always` emits ANSI SGR into the
result; the DISPLAY stream keeps the colour (native `ls` look) while the MODEL context is ANSI-STRIPPED —
the model reads clean text (escape bytes are noise and skew the char count). Drives the real run loop.
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


def _agent(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(gx10, "OpenAI", lambda **kw: object())
    g = gx10.GX10(base_url="http://x/v1", api_key="k", model="m", prompt_path="")
    g.messages = [{"role": "system", "content": "SYS"}]
    monkeypatch.setattr(g, "_classify_thinking", lambda _u: False)
    gx10._CANCEL_EVENT.clear()
    return g


def _run_one_tool_turn(monkeypatch, g, command, stdout):
    """One generation that calls execute_command(command), then a plain second generation that ends the
    turn. Captures every _ui_print line (the DISPLAY stream)."""
    display = []
    monkeypatch.setattr(gx10, "_ui_print", lambda *a, **k: display.append(a[0] if a else ""))
    monkeypatch.setattr(gx10, "_run_model_command_process",
                        lambda *a, **k: types.SimpleNamespace(returncode=0, stdout=stdout, stderr=""))
    calls = [
        ("", [{"id": "c1", "name": "execute_command",
               "arguments": json.dumps({"command": command})}], False, None, {}),
        ("done", [], False, None, {}),
    ]
    monkeypatch.setattr(g, "_generate", lambda think: calls.pop(0))
    g.run("list the files")
    return display


def test_model_context_is_ansi_stripped_display_keeps_colour(monkeypatch, tmp_path, model_sandbox_backend):
    g = _agent(monkeypatch, tmp_path)
    (tmp_path / "d0").mkdir()
    (tmp_path / "f0.txt").write_text("")
    coloured = "total 0\n\x1b[01;34md0\x1b[0m\n\x1b[01;32mf0.txt\x1b[0m"
    display = _run_one_tool_turn(monkeypatch, g, "ls -lA --color=always", coloured)

    # MODEL context: the tool message content carries NO escape bytes, but keeps the visible text + the
    # deterministic count header / Answer line.
    tool_msg = next(m for m in g.messages if m.get("role") == "tool")
    assert "\x1b[" not in tool_msg["content"]
    assert "d0" in tool_msg["content"] and "f0.txt" in tool_msg["content"]
    # a deterministic count header on line 1 + the localized Answer directly under it (exact numbers
    # vary — the engine seeds its own state dirs under tmp_path; the point is the SHAPE survives the strip)
    lines = tool_msg["content"].split("\n")
    header_i = next(i for i, line in enumerate(lines) if gx10._LISTING_HEADER_RE.fullmatch(line))
    assert lines[header_i + 1].startswith("Answer: ")

    # DISPLAY stream: at least one streamed line still carries the raw SGR colour (native ls look).
    assert any("\x1b[" in str(ln) for ln in display), "display lost the colour"
    # a coloured line is streamed WITHOUT the grey wrap — its `⎿`/`     ` prefix stays plain at the start
    coloured_lines = [str(ln) for ln in display if "\x1b[01;3" in str(ln)]
    assert coloured_lines and any(ln.lstrip().startswith(("⎿", "d0", "f0")) or ln.startswith(("  ⎿", "     "))
                                  for ln in coloured_lines)


def test_non_ansi_tool_result_is_fenced_without_ansi(monkeypatch, tmp_path, model_sandbox_backend):
    """A plain command result is unchanged inside the mandatory untrusted-data fence."""
    g = _agent(monkeypatch, tmp_path)
    (tmp_path / "only.txt").write_text("")
    display = _run_one_tool_turn(monkeypatch, g, "echo hi", "hi")
    tool_msg = next(m for m in g.messages if m.get("role") == "tool")
    assert "UNTRUSTED CONTENT" in tool_msg["content"] and "\nhi\n" in tool_msg["content"]
    assert not any("\x1b[01" in str(ln) for ln in display)


def test_ansi_strip_is_scoped_to_execute_command_not_read_file(monkeypatch, tmp_path):
    """#1196 (review MEDIUM): the ANSI strip is scoped to execute_command. A read_file result that
    legitimately CONTAINS escape bytes (a terminal capture / ANSI-art / colour-coded log) reaches the model
    VERBATIM — we never silently alter ingested file content."""
    g = _agent(monkeypatch, tmp_path)
    p = tmp_path / "capture.ans"
    p.write_text("\x1b[31mRED\x1b[0m line", encoding="utf-8")
    monkeypatch.setattr(gx10, "_ui_print", lambda *a, **k: None)
    calls = [
        ("", [{"id": "c1", "name": "read_file", "arguments": json.dumps({"path": str(p)})}], False, None, {}),
        ("done", [], False, None, {}),
    ]
    monkeypatch.setattr(g, "_generate", lambda think: calls.pop(0))
    g.run("read it")
    tool_msg = next(m for m in g.messages if m.get("role") == "tool")
    assert "UNTRUSTED CONTENT" in tool_msg["content"]
    assert "\x1b[31m" in tool_msg["content"]              # escape bytes preserved inside the read_file fence
