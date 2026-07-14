"""Epic #505, S3 — the standalone web-search adapter seam.

Covers the structured SearchOutput contract, the CLI-delegate / mock / unavailable adapters, and
the config-driven builder (cli / brave / mock selection, the Fork 2 server-mode fallback). Pure
and network-free (the CLI adapter rides a fake dispatcher). Maps to spec tests 5-7, 12, 14, 15.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

from websearch_adapters import (  # noqa: E402
    CliDelegateAdapter, MockAdapter, SearchBatch, SearchHit, SearchOutput,
    UnavailableAdapter, build_web_search_adapter,
)


class _FakeDispatcher:
    def __init__(self, web=True, result=None):
        self._web = web
        self._result = result or {"ok": True, "content": "RAW CLI TEXT", "error": None,
                                  "provider_id": "codex-web"}
        self.calls = []

    def has_web_provider(self):
        return self._web

    def web_search(self, query, **kw):
        self.calls.append(query)
        return self._result


# ── SearchOutput contract (spec tests 5, 6, 12) ──────────────────────────────
def test_to_model_text_renders_links_and_strings():
    out = SearchOutput(query="q", results=(
        SearchBatch("b1", (SearchHit("Title A", "https://a.test", "snip"),
                           SearchHit("Title B", "https://b.test"))),
        "a plain note",
    ), duration_ms=12)
    text = out.to_model_text()
    assert 'Web search results for: "q"' in text
    assert "- Title A: https://a.test" in text and "- Title B: https://b.test" in text
    assert "a plain note" in text


def test_to_model_text_handles_no_links():
    out = SearchOutput(query="q", results=(SearchBatch("b1", ()),))
    assert "No links found." in out.to_model_text()


def test_batch_count_skips_string_entries():        # spec test 12
    out = SearchOutput(query="q", results=(
        SearchBatch("b1", (SearchHit("t", "u"),)), "note", SearchBatch("b2", ())))
    assert out.batch_count() == 2
    assert [h.url for h in out.all_hits()] == ["u"]


def test_search_output_is_frozen():                 # read-only-ish: results are immutable tuples
    out = SearchOutput(query="q")
    import pytest
    with pytest.raises(Exception):
        out.query = "x"   # type: ignore[misc]


# ── CliDelegateAdapter (spec tests 6, 7, 14) ─────────────────────────────────
def test_cli_adapter_available_reflects_dispatcher():
    assert CliDelegateAdapter(_FakeDispatcher(web=True)).available() is True
    assert CliDelegateAdapter(_FakeDispatcher(web=False)).available() is False
    assert CliDelegateAdapter(None).available() is False


def test_cli_adapter_wraps_content_as_string_entry():
    disp = _FakeDispatcher(web=True, result={"ok": True, "content": "  RESULTS  ", "error": None})
    out = CliDelegateAdapter(disp).run("hello")
    assert disp.calls == ["hello"]
    assert out.results == ("RESULTS",) and out.query == "hello" and out.duration_ms >= 0


def test_cli_adapter_error_becomes_readable_note():
    disp = _FakeDispatcher(web=True, result={"ok": False, "content": None, "error": "boom"})
    out = CliDelegateAdapter(disp).run("hello")
    assert len(out.results) == 1 and "no result" in out.results[0] and "boom" in out.results[0]


def test_cli_adapter_is_read_only_and_repeatable():   # spec tests 14 + 15 (stateless → concurrency-safe)
    disp = _FakeDispatcher(web=True)
    a = CliDelegateAdapter(disp)
    a.run("one")
    a.run("two")
    assert disp.calls == ["one", "two"]               # no hidden cross-call state in the adapter


# ── MockAdapter (spec test 5) ────────────────────────────────────────────────
def test_mock_adapter_always_available_and_returns_batch():
    out = MockAdapter().run("anything")
    assert MockAdapter().available() is True
    assert out.batch_count() == 1 and out.all_hits() and out.all_hits()[0].url.startswith("http")


# ── builder selection (Fork 2 + Fork 3) ──────────────────────────────────────
def test_builder_defaults_to_disabled():
    a = build_web_search_adapter({}, _FakeDispatcher(web=True))
    assert isinstance(a, UnavailableAdapter) and a.available() is False


def test_builder_selects_mock():
    assert isinstance(build_web_search_adapter(
        {"search": {"enabled": True, "adapter": "mock"}}, None), MockAdapter)


def test_builder_disabled_is_unavailable():
    a = build_web_search_adapter({"search": {"enabled": False}}, _FakeDispatcher(web=True))
    assert isinstance(a, UnavailableAdapter) and a.available() is False


def test_builder_brave_falls_back_to_cli_in_server_mode():   # Fork 2: native search is local-only
    a = build_web_search_adapter({"search": {"enabled": True, "adapter": "brave"}}, _FakeDispatcher(web=True),
                                 runner_mode="none")
    assert isinstance(a, CliDelegateAdapter)


def test_builder_brave_local_unavailable_without_key(monkeypatch):
    monkeypatch.delenv("GX10_SEARCH_API_KEY", raising=False)
    a = build_web_search_adapter(
        {"search": {"enabled": True, "adapter": "brave"}}, None, runner_mode="local")
    assert isinstance(a, UnavailableAdapter) and not a.available()


def test_builder_brave_local_builds_native_adapter_with_key(monkeypatch):
    monkeypatch.setenv("GX10_SEARCH_API_KEY", "secret-key")
    from websearch_brave import BraveAdapter
    a = build_web_search_adapter(
        {"search": {"enabled": True, "adapter": "brave"}}, None, runner_mode="local")
    assert isinstance(a, BraveAdapter) and a.available() is True


def test_builder_brave_tolerates_a_bad_count_value(monkeypatch):
    monkeypatch.setenv("GX10_SEARCH_API_KEY", "secret-key")
    from websearch_brave import BraveAdapter
    # a non-numeric config count must not raise out of the builder (server boot is fail-soft).
    a = build_web_search_adapter(
        {"search": {"enabled": True, "adapter": "brave", "count": "oops"}}, None,
                                 runner_mode="local")
    assert isinstance(a, BraveAdapter)
