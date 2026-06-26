"""Epic #505, S1 — pure web-search input contract + domain normalizer.

Covers the clean-room spec's input rules (query>=2, allowDomains XOR blockDomains, domain
normalization, wildcard reject) as a standalone, network-free, engine-free unit. These map
to spec tests 1-4 and 13 (the wildcard reject, scoped to domain filters per epic #505
Fork 4). The module under test imports nothing from the engine.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import websearch  # noqa: E402
from websearch import validate_web_search_input as v  # noqa: E402


# ── spec test 1 + 2: query length / presence ─────────────────────────────────
def test_query_below_two_chars_is_rejected():
    req, err = v({"query": "a"})
    assert req is None and err and "at least 2" in err


def test_whitespace_only_query_is_rejected():
    req, err = v({"query": "   "})
    assert req is None and err


def test_empty_query_is_rejected():
    req, err = v({"query": ""})
    assert req is None and err


def test_missing_query_is_rejected():
    req, err = v({})
    assert req is None and err


def test_non_string_query_is_rejected():
    req, err = v({"query": 42})
    assert req is None and err


def test_minimal_valid_query_passes():
    req, err = v({"query": "ai"})
    assert err is None and req is not None and req.query == "ai"


def test_query_is_trimmed():
    req, err = v({"query": "  hello world  "})
    assert err is None and req.query == "hello world"


# ── spec test 3: allow XOR block ─────────────────────────────────────────────
def test_allow_and_block_together_are_rejected():
    req, err = v({"query": "qq", "allowDomains": ["a.com"], "blockDomains": ["b.com"]})
    assert req is None and err and "mutually exclusive" in err


def test_allow_only_passes():
    req, err = v({"query": "qq", "allowDomains": ["Example.com"]})
    assert err is None and req.allow_domains == ("example.com",) and req.block_domains == ()


def test_block_only_passes():
    req, err = v({"query": "qq", "blockDomains": ["spam.test"]})
    assert err is None and req.block_domains == ("spam.test",) and req.allow_domains == ()


# ── spec test 4: domain normalization ────────────────────────────────────────
def test_normalize_strips_scheme_path_and_lowercases():
    assert websearch.normalize_domain("HTTPS://Foo.com/Path?x=1") == "foo.com"
    assert websearch.normalize_domain("http://Bar.ORG") == "bar.org"
    assert websearch.normalize_domain("  Example.com  ") == "example.com"


def test_allow_domains_are_normalized():
    req, err = v({"query": "qq", "allowDomains": ["HTTPS://Docs.Python.org/3/"]})
    assert err is None and req.allow_domains == ("docs.python.org",)


def test_non_string_domain_entry_is_rejected():
    req, err = v({"query": "qq", "allowDomains": [123]})
    assert req is None and err and "strings" in err


def test_non_list_domain_filter_is_rejected():
    req, err = v({"query": "qq", "allowDomains": "a.com"})
    assert req is None and err


def test_empty_domain_after_normalization_is_rejected():
    req, err = v({"query": "qq", "blockDomains": ["https://"]})
    assert req is None and err


# ── spec test 13: wildcard reject (scoped to domain filters; epic #505 Fork 4) ─
def test_wildcard_domain_is_rejected():
    req, err = v({"query": "qq", "allowDomains": ["*.foo.com"]})
    assert req is None and err and "wildcard" in err.lower()


def test_question_mark_wildcard_is_rejected():
    req, err = v({"query": "qq", "blockDomains": ["ex?mple.com"]})
    assert req is None and err and "wildcard" in err.lower()


def test_has_wildcard_helper():
    assert websearch.has_wildcard("*.x.com") is True
    assert websearch.has_wildcard("a?b") is True
    assert websearch.has_wildcard("example.com") is False


# ── shape / purity ───────────────────────────────────────────────────────────
def test_request_is_frozen_and_hashable():
    req, _ = v({"query": "qq", "allowDomains": ["a.com"]})
    assert isinstance(hash(req), int)  # frozen dataclass → usable as a key


def test_non_dict_args_are_rejected():
    req, err = v("not a dict")
    assert req is None and err
