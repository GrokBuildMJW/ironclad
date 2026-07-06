"""#1047 (L1): ranged / pattern read_file + search-first schema re-steer.

`read_file` gains start/end (1-based inclusive lines), an optional regex `pattern` (a window around the
first match), and max_chars — so the model reads only the relevant range of a large file instead of the
whole thing. A bad range / unmatched-or-invalid pattern falls back to the existing head+tail cap (never
crashes). The slice logic is mirrored in the ink client (clients/ink runTool.ts) for the local topology.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402


def _numbered(n):
    return "\n".join(f"L{i}" for i in range(1, n + 1))


def test_read_file_ranged_line_range():
    r = gx10._read_file_ranged(_numbered(50), start=5, end=7)
    assert r.startswith("[Ironclad: lines 5-7 of 50]")
    assert r.splitlines()[1:] == ["L5", "L6", "L7"]


def test_read_file_ranged_open_ended():
    r = gx10._read_file_ranged(_numbered(10), start=8)          # end defaults to the last line
    assert r.splitlines()[1:] == ["L8", "L9", "L10"]
    r2 = gx10._read_file_ranged(_numbered(10), end=3)           # start defaults to 1
    assert r2.splitlines()[1:] == ["L1", "L2", "L3"]


def test_read_file_ranged_bad_range_returns_none():
    text = _numbered(50)
    assert gx10._read_file_ranged(text, start=99) is None       # start past EOF
    assert gx10._read_file_ranged(text, start=10, end=5) is None  # end before start
    assert gx10._read_file_ranged(text, start=0) is None        # 1-based; 0 is invalid
    assert gx10._read_file_ranged(text) is None                 # no ranged args → caller uses the normal path


def test_read_file_ranged_pattern_window():
    r = gx10._read_file_ranged(_numbered(50), pattern=r"^L25$")
    assert "lines 5-45 of 50" in r.splitlines()[0]              # a ±20-line window around the match
    assert "L25" in r


def test_read_file_ranged_pattern_no_match_or_invalid_returns_none():
    text = _numbered(50)
    assert gx10._read_file_ranged(text, pattern="ZZZ-NOPE") is None   # no match → fall back
    assert gx10._read_file_ranged(text, pattern="[") is None          # invalid regex → never raises, fall back


def test_read_file_ranged_max_chars_caps_the_slice():
    big = "X" * 1000 + "\n" + "Y" * 1000
    r = gx10._read_file_ranged(big, start=1, end=2, max_chars=200)
    assert "omitted from the slice — capped at 200" in r
    assert len(r) < 400                                          # head+tail of 200 + a short marker


def test_run_tool_read_file_ranged(tmp_path):
    f = tmp_path / "big.txt"
    f.write_text("\n".join(f"row{i}" for i in range(1, 201)), encoding="utf-8")
    out = gx10.run_tool("read_file", {"path": str(f), "start": 50, "end": 52})
    assert out.splitlines() == ["[Ironclad: lines 50-52 of 200]", "row50", "row51", "row52"]


def test_run_tool_read_file_out_of_range_falls_back(tmp_path):
    f = tmp_path / "big.txt"
    f.write_text("\n".join(f"row{i}" for i in range(1, 201)), encoding="utf-8")
    fb = gx10.run_tool("read_file", {"path": str(f), "start": 9999})   # bad range → head+tail fallback, no crash
    assert fb.startswith("row1") and "[Ironclad: lines" not in fb
    whole = gx10.run_tool("read_file", {"path": str(f)})               # no ranged args → unchanged whole-file read
    assert whole.startswith("row1") and "[Ironclad: lines" not in whole


def test_read_file_schema_advertises_the_ranged_params():
    tool = next(t for t in gx10.TOOLS if t["function"]["name"] == "read_file")
    props = tool["function"]["parameters"]["properties"]
    assert {"path", "start", "end", "max_chars", "pattern"} <= set(props)
    assert "search_files" in tool["function"]["description"]     # search-first re-steer
