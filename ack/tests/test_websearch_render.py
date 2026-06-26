"""Epic #505, S9 — the server emits a `[search]` control frame.

The web_search handler emits a `[search] n=… ms=…` frame via `_ui_print` (the [perf]/[agent]
pattern); every client routes it to the status footer ("web N · Xms") and strips it from the chat.
This covers the producer side; the ink consumer is covered by clients/ink/test/route.test.ts.
"""
from __future__ import annotations

import pathlib
import sys

_ENGINE = pathlib.Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402
from websearch_adapters import MockAdapter  # noqa: E402


def test_handler_emits_search_frame(monkeypatch):
    frames = []
    monkeypatch.setattr(gx10, "_WEBSEARCH", MockAdapter())
    monkeypatch.setattr(gx10, "_is_sealed_profile", lambda: False)
    monkeypatch.setattr(gx10, "_ui_print", lambda *a, **k: frames.append(" ".join(str(x) for x in a)))
    gx10.run_tool("web_search", {"query": "latest ai"})
    hit = [f for f in frames if "[search]" in f]
    assert hit, "no [search] control frame emitted"
    # spec test 8 (the query the search ran for) + 9 (results received): the single post-completion
    # frame carries q + n + ms (the synchronous backends have no separate query-start event).
    assert 'q="latest ai"' in hit[0] and "n=" in hit[0] and "ms=" in hit[0]


def test_search_frame_not_emitted_when_blocked(monkeypatch):
    # under sealed (no override) the trust gate refuses BEFORE the search → no frame, no egress.
    frames = []
    monkeypatch.setattr(gx10, "_WEBSEARCH", MockAdapter())
    monkeypatch.setattr(gx10, "_is_sealed_profile", lambda: True)
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", {"security": {"web_in_sealed": False}})
    monkeypatch.setattr(gx10, "_ui_print", lambda *a, **k: frames.append(" ".join(str(x) for x in a)))
    out = gx10.run_tool("web_search", {"query": "latest ai"})
    assert "blocked" in out.lower()
    assert not any("[search]" in f for f in frames)
