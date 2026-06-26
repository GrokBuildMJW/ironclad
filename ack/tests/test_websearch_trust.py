"""Epic #505, S7 — the sealed trust gate for web_search.

Outbound web_search is blocked under the `sealed` (sovereign/loopback) trust profile unless the
operator opts in via security.web_in_sealed. The block is enforced at BOTH the offer-gate (not
offered) and the exec re-gate (a direct run_tool / manual `/tool` / hallucinated call gets a
deterministic refusal, never a silent egress). web_search stays out of LOCAL_TOOL_NAMES so a thin
client cannot bypass the server-side gate. Maps to the spec's permission-gating requirement.
"""
from __future__ import annotations

import pathlib
import sys

_ENGINE = pathlib.Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402
from websearch_adapters import MockAdapter  # noqa: E402


def _seal(monkeypatch, sealed, *, override=False):
    monkeypatch.setattr(gx10, "_WEBSEARCH", MockAdapter())      # a usable adapter is present
    monkeypatch.setattr(gx10, "_is_sealed_profile", lambda: sealed)
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", {"security": {"web_in_sealed": override}})


def test_sealed_does_not_offer_web_search(monkeypatch):
    _seal(monkeypatch, True)
    assert all(t["function"]["name"] != "web_search" for t in gx10._effective_tools())


def test_open_offers_web_search(monkeypatch):
    _seal(monkeypatch, False)
    assert any(t["function"]["name"] == "web_search" for t in gx10._effective_tools())


def test_sealed_exec_returns_specific_block_message(monkeypatch):
    # the exec re-gate must fire even on a direct run_tool call (manual /tool, hallucinated, etc.)
    _seal(monkeypatch, True)
    out = gx10.run_tool("web_search", {"query": "latest ai"})
    assert "sealed" in out.lower() and "blocked" in out.lower() and "Sources:" not in out


def test_sealed_override_allows_exec(monkeypatch):
    _seal(monkeypatch, True, override=True)
    out = gx10.run_tool("web_search", {"query": "latest ai"})
    assert "blocked" not in out.lower() and "Sources:" in out         # runs through the adapter


def test_open_profile_allows_exec(monkeypatch):
    _seal(monkeypatch, False)
    out = gx10.run_tool("web_search", {"query": "latest ai"})
    assert "blocked" not in out.lower() and "Sources:" in out


def test_steer_silent_under_sealed_unless_overridden(monkeypatch):
    _seal(monkeypatch, True)
    assert gx10._websearch_steer("what is the latest news") == ""     # no dead hint under sealed
    _seal(monkeypatch, True, override=True)
    assert "web_search" in gx10._websearch_steer("what is the latest news")


def test_web_search_is_not_a_local_tool():
    # server-side authoritative: a thin/ink client cannot bypass the trust gate via X-Local-Tools.
    assert "web_search" not in gx10.LOCAL_TOOL_NAMES
