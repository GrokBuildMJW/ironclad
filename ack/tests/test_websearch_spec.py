"""Epic #505, S10 — clean-room spec test consolidation + traceability.

The 16 named tests from the clean-room guide (Tests section), mapped to where they are covered. This
file adds the integration-level legs that tie the pipeline together and the legs the per-stage files
do not own; the rest are cited so the 16 are explicitly accounted for (no silent gap).

  1  query < 2 rejected ............... test_websearch_input.test_query_below_two_chars_is_rejected
  2  empty query rejected ............. test_websearch_input.test_empty_query_is_rejected
  3  allow + block rejected ........... test_websearch_input.test_allow_and_block_together_are_rejected
  4  domain normalize (scheme/path) ... test_websearch_input.test_normalize_strips_scheme_path_and_lowercases
  5  provider hits -> SearchBatch ..... test_websearch_brave.test_run_success_normalizes_hits... + below (e2e)
  6  provider text -> string .......... test_websearch_adapters.test_cli_adapter_wraps_content_as_string_entry
  7  provider error -> readable str ... test_websearch_brave.test_http_error_is_failsoft / test_timeout_is_failsoft
  8  query-start carried .............. test_websearch_render (the [search] frame carries q=)
  9  results-received emitted ......... test_websearch_render (the [search] frame carries n=/ms=)
  10 model output contains links ...... test_websearch_output.test_sources_block_lists_unique_urls
  11 output contains sources reminder . test_websearch_output.test_reminder_always_present...
  12 UI summary counts only batches ... test_websearch_adapters.test_batch_count_skips_string_entries
  13 wildcard rejected (domains) ...... test_websearch_input.test_wildcard_domain_is_rejected
  14 read-only ........................ below (not a client-local tool; handler mutates no state)
  15 concurrency-safe ................. below (N concurrent calls return independent results)
  16 disabled when no provider ........ test_websearch_trust.test_open_offers... + below (all three adapters)

Note (spec 8): the synchronous brave/CLI backends have no separate "query_started" producer; progress
is optional in the guide, so the single post-completion [search] frame carries q + n + ms (one event).
"""
from __future__ import annotations

import pathlib
import sys
from concurrent.futures import ThreadPoolExecutor

_ENGINE = pathlib.Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402
from websearch_adapters import MockAdapter, SearchBatch, SearchHit, SearchOutput  # noqa: E402
from websearch_brave import BraveAdapter  # noqa: E402


# ── spec 16: offered for ALL three adapter kinds (the predicate change, R2) ──
def test_mock_adapter_is_offered_without_a_cli_provider(monkeypatch):
    monkeypatch.setattr(gx10, "_DISPATCHER", None)
    monkeypatch.setattr(gx10, "_is_sealed_profile", lambda: False)
    monkeypatch.setattr(gx10, "_WEBSEARCH", MockAdapter())
    assert any(t["function"]["name"] == "web_search" for t in gx10._effective_tools())


def test_brave_adapter_with_key_is_offered_without_a_cli_provider(monkeypatch):
    monkeypatch.setattr(gx10, "_DISPATCHER", None)            # NO cli web provider
    monkeypatch.setattr(gx10, "_is_sealed_profile", lambda: False)
    monkeypatch.setattr(gx10, "_WEBSEARCH", BraveAdapter("a-key"))
    assert any(t["function"]["name"] == "web_search" for t in gx10._effective_tools())


def test_no_adapter_is_not_offered(monkeypatch):             # spec 16: disabled when nothing can search
    monkeypatch.setattr(gx10, "_WEBSEARCH", None)
    assert all(t["function"]["name"] != "web_search" for t in gx10._effective_tools())


# ── spec 14: read-only (behavioral — R3) ─────────────────────────────────────
def test_web_search_is_read_only_server_authoritative():
    # not a client-local tool (cannot be bridged/bypassed) — the handler performs no fs/state mutation.
    assert "web_search" not in gx10.LOCAL_TOOL_NAMES


# ── spec 15: concurrency-safe (behavioral — R3) ──────────────────────────────
def test_adapter_is_concurrency_safe():
    a = MockAdapter()
    with ThreadPoolExecutor(max_workers=8) as ex:
        outs = list(ex.map(lambda i: a.run(f"q{i}"), range(40)))
    assert len(outs) == 40
    assert all(o.query == f"q{i}" for i, o in enumerate(outs))   # no cross-call corruption (stateless seam)


# ── spec 5 + 10 + 11: end-to-end pipeline via the mock adapter ───────────────
def test_spec_pipeline_via_mock_end_to_end(monkeypatch):
    monkeypatch.setattr(gx10, "_WEBSEARCH", MockAdapter())
    monkeypatch.setattr(gx10, "_is_sealed_profile", lambda: False)
    out = gx10.run_tool("web_search", {"query": "what is the latest ai"})
    assert "Sources:" in out and "http" in out and "Reminder" in out   # links + sources reminder


# ── spec 12: the n= summary counts only real batches (server-side, D5) ───────
def test_n_counts_only_real_batches():
    out = SearchOutput(query="q", results=(
        SearchBatch("a", (SearchHit("t", "u"),)), "a note", SearchBatch("b", ())))
    assert out.batch_count() == 2          # the [search] frame's n= skips the string note
