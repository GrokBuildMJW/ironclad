"""Typed design metadata normalization."""
from __future__ import annotations

from ack.ace.constraint_types import normalize_language, normalize_network, parse_typed


def test_normalize_language_aliases_and_unknowns():
    assert normalize_language("py") == "python"
    assert normalize_language("Python3") == "python"
    assert normalize_language("rs") == "rust"
    assert normalize_language("typescript") == "typescript"
    assert normalize_language("klingon") is None
    assert normalize_language(True) is None


def test_normalize_network_tokens():
    assert normalize_network("none") is False
    assert normalize_network("forbidden") is False
    assert normalize_network("allowed") is True
    assert normalize_network(True) is True
    assert normalize_network("maybe") is None


def test_parse_typed_from_frontmatter_and_text():
    assert parse_typed({"language": "py", "network": "none"}) == {
        "language": "python",
        "network": False,
    }
    assert parse_typed("---\nlanguage: rust\nnetwork: allowed\n---\nbody") == {
        "language": "rust",
        "network": True,
    }
    assert parse_typed({"language": "unknown", "network": "maybe"}) == {}
