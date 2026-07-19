"""Phase-e: the in-engine ``parallel_reason`` tool (gx10.py).

The tool is offered only when the governed fan-out workers exist (server-side), routes
to ``ReasoningWorkers.fanout``, and renders results back into the turn in input order
with per-item error isolation. No model needed — the workers handle is stubbed.
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


class _StubWorkers:
    """Captures fanout args; echoes each item, fails any item containing 'bad'."""
    def __init__(self):
        self.calls = []

    def fanout(self, items, *, system=None, contexts=None, max_tokens=None, think=True):
        self.calls.append({"items": list(items), "system": system, "contexts": contexts,
                           "max_tokens": max_tokens, "think": think})
        out = []
        for it in items:
            if "bad" in it:
                out.append({"ok": False, "content": None, "error": f"boom:{it}"})
            else:
                out.append({"ok": True, "content": f"R:{it}", "error": None})
        return out


@pytest.fixture(autouse=True)
def _restore_workers():
    prev = gx10._WORKERS
    yield
    gx10._WORKERS = prev


def test_tool_offered_only_with_workers():
    gx10._WORKERS = None
    assert all(t["function"]["name"] != "parallel_reason"
               for t in gx10._effective_tools())
    gx10._WORKERS = _StubWorkers()
    assert any(t["function"]["name"] == "parallel_reason"
               for t in gx10._effective_tools())


def test_unavailable_without_workers():
    gx10._WORKERS = None
    out = gx10.run_tool("parallel_reason", {"items": ["a"]})
    assert "unavailable" in out


def test_routes_to_fanout_and_formats():
    w = _StubWorkers()
    gx10._WORKERS = w
    out = gx10.run_tool("parallel_reason", {
        "items": ["x", "y", "z"],
        "instruction": "summarise",
        "max_tokens": 512,
    })
    # forwarded correctly
    assert w.calls[0]["items"] == ["x", "y", "z"]
    assert w.calls[0]["system"] == "summarise"
    assert w.calls[0]["contexts"] is None        # §3c MAP off by default → stateless fan-out
    assert w.calls[0]["max_tokens"] == 512
    assert w.calls[0]["think"] is True
    # rendered in order, with the ok-count header
    assert out.splitlines()[0] == "[parallel_reason] 3/3 ok"
    assert "[1] R:x" in out and "[2] R:y" in out and "[3] R:z" in out


def test_error_isolation_in_render():
    gx10._WORKERS = _StubWorkers()
    out = gx10.run_tool("parallel_reason", {"items": ["good", "bad-one", "fine"]})
    assert out.splitlines()[0] == "[parallel_reason] 2/3 ok"
    assert "[2] ERROR: boom:bad-one" in out


def test_format_parallel_all_empty_ok_branches():
    out = gx10._format_parallel([
        {"ok": True, "content": "", "error": None},
        {"ok": True, "content": None, "error": None},
    ])
    assert out == (
        "[parallel_reason] 0/2 ok\n\n"
        "[1] EMPTY: branch returned no content\n\n"
        "[2] EMPTY: branch returned no content"
    )


def test_format_parallel_mixed_results_preserve_order():
    out = gx10._format_parallel([
        {"ok": True, "content": " first ", "error": None},
        {"ok": True, "content": "", "error": None},
        {"ok": False, "content": None, "error": "boom"},
        {"ok": True, "content": "last", "error": None},
    ])
    assert out == (
        "[parallel_reason] 2/4 ok\n\n"
        "[1] first\n\n"
        "[2] EMPTY: branch returned no content\n\n"
        "[3] ERROR: boom\n\n"
        "[4] last"
    )


def test_format_parallel_whitespace_only_content_is_empty():
    out = gx10._format_parallel([{"ok": True, "content": " \t\r\n ", "error": None}])
    assert out == (
        "[parallel_reason] 0/1 ok\n\n"
        "[1] EMPTY: branch returned no content"
    )


def test_format_parallel_all_content_is_byte_identical():
    out = gx10._format_parallel([
        {"ok": True, "content": " alpha ", "error": None},
        {"ok": True, "content": "beta", "error": None},
    ])
    assert out == "[parallel_reason] 2/2 ok\n\n[1] alpha\n\n[2] beta"


def test_validation_rejects_bad_items():
    gx10._WORKERS = _StubWorkers()
    assert "non-empty list" in gx10.run_tool("parallel_reason", {"items": []})
    assert "non-empty list" in gx10.run_tool("parallel_reason", {"items": "notalist"})
    assert "non-empty list" in gx10.run_tool("parallel_reason", {"items": [1, 2]})
