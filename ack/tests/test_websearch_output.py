"""Epic #505, S5 — the model-facing output formatter: deterministic Sources block + max cap.

Covers the 'always write sources' invariant (the reminder is appended after the cap, so it is
never truncated away) and the maxOutputCharacters guard. Maps to spec tests 10 (output contains
links) and 11 (output contains the sources reminder).
"""
from __future__ import annotations

import pathlib
import sys

_ENGINE = pathlib.Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

from websearch_adapters import (  # noqa: E402
    DEFAULT_MAX_OUTPUT_CHARS, SearchBatch, SearchHit, SearchOutput, _SOURCES_REMINDER,
    format_for_model,
)


def _out(hits=(), strings=()):
    results = []
    if hits:
        results.append(SearchBatch("b", tuple(hits)))
    results.extend(strings)
    return SearchOutput(query="q", results=tuple(results))


def test_sources_block_lists_unique_urls():        # spec test 10
    out = _out(hits=(SearchHit("A", "https://a.test"), SearchHit("B", "https://b.test"),
                     SearchHit("A-again", "https://a.test")))
    text = format_for_model(out)
    assert "Sources:" in text
    sources = text.split("Sources:")[1]
    assert "- https://a.test" in sources and "- https://b.test" in sources
    assert sources.count("- https://a.test") == 1     # deduplicated


def test_reminder_always_present_even_without_links():   # spec test 11 (CLI-delegate path)
    text = format_for_model(_out(strings=("opaque CLI text",)))
    assert text.endswith(_SOURCES_REMINDER) and "Sources:" not in text


def test_reminder_present_with_links():             # spec test 11 (structured path)
    assert format_for_model(_out(hits=(SearchHit("A", "https://a.test"),))).endswith(_SOURCES_REMINDER)


def test_cap_truncates_body_but_keeps_reminder():
    out = _out(strings=("x" * 5000,))
    text = format_for_model(out, max_output_chars=200)
    assert text.endswith(_SOURCES_REMINDER)
    assert len(text) <= 200 + len(_SOURCES_REMINDER) + 2   # cap applies to body; reminder survives


def test_bad_or_zero_cap_falls_back_to_default():
    out = _out(strings=("hi",))
    assert format_for_model(out, max_output_chars="oops").endswith(_SOURCES_REMINDER)
    assert format_for_model(out, max_output_chars=0).endswith(_SOURCES_REMINDER)
    assert format_for_model(out, max_output_chars=None).endswith(_SOURCES_REMINDER)


def test_default_cap_is_one_hundred_thousand():
    assert DEFAULT_MAX_OUTPUT_CHARS == 100_000
