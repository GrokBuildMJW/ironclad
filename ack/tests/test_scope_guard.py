from __future__ import annotations

import importlib.util
from pathlib import Path

# Load the pure guard module DIRECTLY by path — do NOT put memory-service/ on sys.path: it holds an
# ``app.py`` that would shadow the engine's ``app`` module for the rest of the pytest session.
_SG_PATH = Path(__file__).resolve().parents[2] / "memory-service" / "scope_guard.py"
_spec = importlib.util.spec_from_file_location("scope_guard", _SG_PATH)
scope_guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scope_guard)


def test_requires_agent_id():
    """Missing or blank agent_id must be refused with an error mentioning agent_id."""
    for bad in (None, "", "   "):
        result = scope_guard.require_scope(bad)
        assert result is not None
        assert isinstance(result, str)
        assert "agent_id" in result


def test_valid_agent_id_ok():
    """A non-blank agent_id with no run_id is well-scoped."""
    assert scope_guard.require_scope("abc123") is None
    assert scope_guard.require_scope("abc123", None) is None
    assert scope_guard.require_scope("abc123", "") is None
    assert scope_guard.require_scope("abc123", "   ") is None


def test_rejects_run_id_as_isolation():
    """run_id must not be used as an isolation key."""
    result = scope_guard.require_scope("abc123", "run-1")
    assert result is not None
    assert isinstance(result, str)
    assert "run_id" in result


def test_non_string_agent_id_rejected():
    """Non-string agent_id values are rejected."""
    assert scope_guard.require_scope(123) is not None
