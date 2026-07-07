"""#1154 (epic #1144): `_TableLineRenderer` re-emits pipe tables as PROPER GFM markdown (pipes + a `|---|`
separator the model is told to omit) for the markdown-rendering client to render as a box, and passes
bold/code/etc. through unchanged — instead of collapsing tables to pipe-less aligned columns and stripping
`**` (a pre-markdown-client leftover the Ink client could only show as flat text).
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


def _render(text: str) -> list:
    out: list = []
    r = gx10._TableLineRenderer(out.append)
    r.feed(text)
    r.flush()
    return out


def _is_sep(line: str) -> bool:
    s = line.replace(" ", "")
    return bool(s) and set(s) <= set("|-") and "-" in s


def test_separatorless_pipe_table_reemitted_as_proper_gfm():
    out = _render("| A | B |\n| 1 | 2 |\n| 3 | 4 |\n")
    joined = "\n".join(out)
    assert "| A | B |" in joined                       # pipes preserved (not collapsed to aligned columns)
    assert sum(1 for l in out if _is_sep(l)) == 1       # exactly one `|---|` separator inserted
    assert "| 1 | 2 |" in joined and "| 3 | 4 |" in joined


def test_bold_and_code_pass_through_unchanged():
    out = _render("Intro mit **fett** und `code`.\n")
    assert out == ["Intro mit **fett** und `code`."]   # NOT stripped — the client renders them


def test_wellformed_table_keeps_a_single_separator():
    out = _render("| A | B |\n|---|---|\n| 1 | 2 |\n")
    assert sum(1 for l in out if _is_sep(l)) == 1       # the model's own separator is dropped, ours is the one
    assert "| A | B |" in out and "| 1 | 2 |" in out


def test_non_table_text_passes_through_verbatim():
    out = _render("just a normal line\nand another\n")
    assert out == ["just a normal line", "and another"]
