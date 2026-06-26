"""Epic #505, S4 — the native Brave HTTP adapter, exercised network-free via an injected opener.

Covers request building (endpoint, token header, count), the domain ``site:`` operators, response
normalization, the fail-soft HTTP/timeout paths, and the vendor-confinement invariant (the Brave
literals live only in websearch_brave.py). Maps to spec tests 5, 7, and the read-only/concurrency
properties. No live network.
"""
from __future__ import annotations

import json
import pathlib
import sys
import urllib.error
import urllib.parse

_ENGINE = pathlib.Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

from websearch_brave import (  # noqa: E402
    BraveAdapter, _ENDPOINT, _TOKEN_HEADER, _build_query, _normalize,
)


class _Resp:
    def __init__(self, body):
        self._b = body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._b


def _capturing_opener(body, cap):
    def _open(req, timeout=None):
        cap["url"] = req.full_url
        cap["headers"] = {k: v for k, v in req.header_items()}
        cap["timeout"] = timeout
        return _Resp(body)
    return _open


_OK = json.dumps({"web": {"results": [
    {"title": "Python", "url": "https://python.org", "description": "the site"},
    {"title": "Docs", "url": "https://docs.python.org"},
]}})


# ── availability + key handling ──────────────────────────────────────────────
def test_available_reflects_key():
    assert BraveAdapter("k").available() is True
    assert BraveAdapter("").available() is False


def test_run_without_key_returns_note():
    out = BraveAdapter("").run("query")
    assert "key is not set" in out.results[0]


# ── request building + normalization (spec test 5) ───────────────────────────
def test_run_success_normalizes_hits_and_builds_request():
    cap = {}
    out = BraveAdapter("secret", opener=_capturing_opener(_OK, cap)).run("python")
    assert out.batch_count() == 1
    assert [h.url for h in out.all_hits()] == ["https://python.org", "https://docs.python.org"]
    assert out.all_hits()[0].title == "Python" and out.all_hits()[0].snippet == "the site"
    assert cap["url"].startswith(_ENDPOINT) and "q=python" in cap["url"] and "count=" in cap["url"]
    assert "secret" in cap["headers"].values()              # the token header carries the key
    assert out.duration_ms >= 0


def test_allow_domains_use_site_operator():
    cap = {}
    BraveAdapter("k", opener=_capturing_opener(_OK, cap)).run("ai", allow_domains=("example.com",))
    assert "site:example.com" in urllib.parse.unquote_plus(cap["url"])


def test_block_domains_use_negated_site_operator():
    assert _build_query("ai", (), ("spam.com",)) == "ai -site:spam.com"


def test_allow_multiple_domains_or_joined():
    assert _build_query("ai", ("a.com", "b.com"), ()) == "ai (site:a.com OR site:b.com)"


def test_empty_results_yield_empty_batch():
    out = BraveAdapter("k", opener=_capturing_opener(json.dumps({"web": {"results": []}}), {})).run("q")
    assert out.batch_count() == 1 and out.all_hits() == []


# ── fail-soft (spec test 7): an error becomes a readable note, never a raise ──
def test_http_error_is_failsoft():
    def _err(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, None)
    out = BraveAdapter("k", opener=_err).run("q")
    assert "search failed" in out.results[0] and "429" in out.results[0]


def test_timeout_is_failsoft():
    def _slow(req, timeout=None):
        raise TimeoutError("slow")
    out = BraveAdapter("k", opener=_slow).run("q")
    assert "could not be completed" in out.results[0]


def test_malformed_json_is_failsoft():
    out = BraveAdapter("k", opener=_capturing_opener("not json", {})).run("q")
    assert "could not be completed" in out.results[0]


# ── defensive normalization ──────────────────────────────────────────────────
def test_normalize_is_defensive():
    assert _normalize({}) == () and _normalize({"web": {}}) == () and _normalize(None) == ()
    assert _normalize({"web": {"results": [{"title": "no url"}]}}) == ()      # skips url-less entries


# ── vendor confinement (epic #505 S4 / R1) ───────────────────────────────────
def test_brave_literals_confined_to_the_adapter_module():
    for py in _ENGINE.glob("*.py"):
        if py.name == "websearch_brave.py":
            continue
        text = py.read_text(encoding="utf-8", errors="replace")
        assert "api.search.brave.com" not in text, f"Brave host literal leaked into {py.name}"
        assert _TOKEN_HEADER not in text, f"Brave token header leaked into {py.name}"
