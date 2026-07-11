"""Pure typed-constraint allow-list + hard/soft classifier (#1341 / epic #1344 S5)."""
from __future__ import annotations

import pytest

from ack.ace.constraint_types import (
    HARD,
    SUGGESTED,
    TYPED_KEYS,
    body_states_typed_constraint,
    classify,
    has_constraints_marker,
    normalize_language,
    normalize_network,
    parse_typed,
)


# --------------------------------------------------------------------------- #
# Language normalization
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("py", "python"),
        ("Python3", "python"),
        ("PYTHON", "python"),
        ("rs", "rust"),
        ("Rust", "rust"),
        ("js", "javascript"),
        ("node", "javascript"),
        ("JavaScript", "javascript"),
        ("ts", "typescript"),
        ("TypeScript", "typescript"),
        ("go", "go"),
        ("golang", "go"),
        ("  py  ", "python"),
    ],
)
def test_normalize_language_aliases(raw, expected):
    assert normalize_language(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", None, "klingon", "cobol", 42, True, False])
def test_normalize_language_unknown_is_none(raw):
    assert normalize_language(raw) is None


# --------------------------------------------------------------------------- #
# Network normalization
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("none", False),
        ("no", False),
        ("off", False),
        ("false", False),
        ("0", False),
        ("forbidden", False),
        ("NONE", False),
        ("allowed", True),
        ("yes", True),
        ("on", True),
        ("true", True),
        ("1", True),
        ("ALLOWED", True),
        (False, False),
        (True, True),
        (0, False),
        (1, True),
    ],
)
def test_normalize_network_tokens(raw, expected):
    assert normalize_network(raw) is expected


@pytest.mark.parametrize("raw", ["", "maybe", "wifi", None, 2, "sometimes"])
def test_normalize_network_unknown_is_none(raw):
    assert normalize_network(raw) is None


# --------------------------------------------------------------------------- #
# parse_typed (deterministic, never raises)
# --------------------------------------------------------------------------- #


def test_parse_typed_from_frontmatter_dict():
    assert parse_typed({"language": "py", "network": "none", "title": "x"}) == {
        "language": "python",
        "network": False,
    }


def test_parse_typed_from_markdown_document():
    text = (
        "---\n"
        "type: decision\n"
        "language: rs\n"
        "network: allowed\n"
        "---\n"
        "body\n"
    )
    assert parse_typed(text) == {"language": "rust", "network": True}


def test_parse_typed_body_lines_and_unknowns():
    assert parse_typed("language: klingon\nnetwork: maybe\n") == {}
    assert parse_typed("language: go\nother: x\n") == {"language": "go"}
    assert parse_typed(None) == {}
    assert parse_typed("") == {}


def test_parse_typed_never_raises_on_hostile_input():
    class Boom:
        def __str__(self):
            raise RuntimeError("boom")

    assert parse_typed(Boom()) == {}  # type: ignore[arg-type]


def test_typed_keys_are_frozen_allow_list():
    assert TYPED_KEYS == ("language", "network")


# --------------------------------------------------------------------------- #
# body_states_typed_constraint (conservative DE/EN capture-completeness detector)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("Sprache: Python", frozenset({"language"})),
        ("language: go", frozenset({"language"})),
        ("Programmiersprache: Go", frozenset({"language"})),
        ("Programmiersprache: Python", frozenset({"language"})),
        ("Programmiersprache Rust", frozenset({"language"})),
        ("implement in Rust", frozenset({"language"})),
        ("implemented in Rust", frozenset({"language"})),
        ("write it in Python", frozenset({"language"})),
        ("coded in Go", frozenset({"language"})),
        ("built using TypeScript", frozenset({"language"})),
        ("Python only", frozenset({"language"})),
        ("nur Python", frozenset({"language"})),
        ("must be Python", frozenset({"language"})),
        ("muss Python sein", frozenset({"language"})),
        ("written in TypeScript", frozenset({"language"})),
        ("written in Go", frozenset({"language"})),
        ("language: js", frozenset({"language"})),
        ("geschrieben in Python", frozenset({"language"})),
        ("geschrieben in golang", frozenset({"language"})),
        ("use Python", frozenset({"language"})),
        ("verwende Python", frozenset({"language"})),
        ("verwende Rust", frozenset({"language"})),
        ("nutze Rust", frozenset({"language"})),
        ("must be node", frozenset({"language"})),
        ("muss ts sein", frozenset({"language"})),
        ("requires python3", frozenset({"language"})),
    ],
)
def test_body_states_typed_constraint_language_qualified(body, expected):
    assert body_states_typed_constraint(body) == expected


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("no network access", frozenset({"network"})),
        ("no network", frozenset({"network"})),
        ("no internet access", frozenset({"network"})),
        ("no internet", frozenset({"network"})),
        ("no online access", frozenset({"network"})),
        ("no external network access", frozenset({"network"})),
        ("no outbound network", frozenset({"network"})),
        ("no external internet", frozenset({"network"})),
        ("offline only", frozenset({"network"})),
        ("offline-only", frozenset({"network"})),
        ("muss offline", frozenset({"network"})),
        ("must be offline", frozenset({"network"})),
        ("must stay offline", frozenset({"network"})),
        ("must run offline", frozenset({"network"})),
        ("runs offline", frozenset({"network"})),
        ("run offline", frozenset({"network"})),
        ("network forbidden", frozenset({"network"})),
        ("network is forbidden", frozenset({"network"})),
        ("network access is forbidden", frozenset({"network"})),
        ("network access not allowed", frozenset({"network"})),
        ("without network access", frozenset({"network"})),
        ("without internet access", frozenset({"network"})),
        ("without internet", frozenset({"network"})),
        ("network: none", frozenset({"network"})),
        ("network=none", frozenset({"network"})),
        ("network: false", frozenset({"network"})),
        ("network=off", frozenset({"network"})),
        ("kein Netzwerk", frozenset({"network"})),
        ("keine externen Netzwerkzugriffe", frozenset({"network"})),
        ("keine Netzwerkverbindung", frozenset({"network"})),
        ("kein Netzwerkzugang", frozenset({"network"})),
        ("kein Internetzugriff", frozenset({"network"})),
        ("keine Internetverbindung", frozenset({"network"})),
        ("ohne Netzwerk", frozenset({"network"})),
        ("ohne Netz", frozenset({"network"})),
        ("ohne Internet", frozenset({"network"})),
        ("Netzwerk verboten", frozenset({"network"})),
        ("Internet verboten", frozenset({"network"})),
    ],
)
def test_body_states_typed_constraint_network_signals(body, expected):
    assert body_states_typed_constraint(body) == expected


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("network is allowed", frozenset()),
        ("online allowed", frozenset()),
        ("online access not allowed", frozenset()),
        ("must be online", frozenset()),
        ("keine Panik, das Netzwerk ist verfügbar", frozenset()),
    ],
)
def test_body_states_typed_constraint_network_presence(body, expected):
    assert body_states_typed_constraint(body) == expected


@pytest.mark.parametrize(
    "body",
    [
        "reads Python files",
        "parses JavaScript input",
        "Python tests",
        "Rust examples",
        "Document Python-only examples",
        "language: java",
        "using Go",
        "in Python",
        "using Go modules",
        "written in Go modules",
        "geschrieben in Go modules",
        "written in python packages",
        "using node modules",
        "using go.mod",
        "in go.mod",
        "using Go-modules",
        "in python path",
        "in the go module registry",
        "use python packages",
        "drop-in Python replacement",
        "built-in Go support",
        "go-live window",
        "changes in Go were reverted",
        "built-in typescript support",
        "using rust analyzer",
        "using go version manager",
        "verwende Python-Pakete",
        "nutze Go-Modul",
        "keine externen Abhängigkeiten",
        "keine externen Bibliotheken",
        "keine externen Pakete",
        "keine Verbindung zur DB",
        "keine VPN-Verbindung nötig",
        "the offline docs generator",
        "bring the node online",
        "Online documentation is allowed as a reference source",
        "Online help is allowed during onboarding",
        "online mode is allowed for demos",
        "network online status in the dashboard",
        "bring services network online",
        "network is allowed",
        "online allowed",
        "must be online",
        "online access not allowed",
        "kein Problem mit dem Netzwerk",
        "keine Ahnung vom Netzwerk",
        "kein Interesse am Netzwerk",
        "kein Problem mit dem Netzwerk-Setup",
    ],
)
def test_body_states_typed_constraint_rejects_incidental_or_unsupported(body):
    assert body_states_typed_constraint(body) == frozenset()


# --------------------------------------------------------------------------- #
# classify (HARD vs SUGGESTED)
# --------------------------------------------------------------------------- #


def test_classify_hard_for_explicit_marker():
    assert classify(explicit_marker=True) == HARD
    assert classify(explicit_marker=True, source="suggested") == HARD


def test_classify_hard_for_typed_param():
    assert classify(typed_supplied=True) == HARD
    assert classify(typed_supplied=True, source="") == HARD


def test_classify_suggested_for_heuristic_only():
    assert classify(source="suggested") == SUGGESTED
    assert classify(typed_supplied=True, source="suggested") == SUGGESTED
    assert classify(explicit_marker=False, typed_supplied=False, source="suggested") == SUGGESTED


def test_classify_plain_capture_defaults_hard():
    assert classify() == HARD


def test_has_constraints_marker():
    assert has_constraints_marker("Constraints: Python only") is True
    assert has_constraints_marker("  constraints: stay local\n") is True
    assert has_constraints_marker("no marker here") is False
    assert has_constraints_marker("") is False
