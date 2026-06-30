"""Shared client command routing (engine/commands.py).

Locks the rule the REPL and the TUI both rely on: `/command` is a command (local or
forwarded to the server), bare `exit`/`quit` leaves, everything else is a turn.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

from commands import classify  # noqa: E402


def test_plain_text_is_a_turn():
    assert classify("was ist 2+2?") == ("turn", "", "was ist 2+2?")


def test_empty():
    assert classify("   ")[0] == "empty"


def test_bare_exit_quit_are_local():
    assert classify("exit") == ("local", "exit", "exit")
    assert classify("QUIT") == ("local", "exit", "quit")


def test_local_commands():
    for c in ("tasks", "pending", "work", "health", "doctor", "help"):   # DOCTOR (#503): /doctor is local now
        kind, name, payload = classify(f"/{c}")
        assert (kind, name) == ("local", c)
    # with args
    assert classify("/auto on") == ("local", "auto", "auto on")


def test_server_commands_forwarded_without_slash():
    # status/config/clear/read/ls/watcher/... go to the orchestrator, slash stripped
    assert classify("/status") == ("server", "status", "status")
    assert classify("/watcher on") == ("server", "watcher", "watcher on")
    assert classify("/read foo.md") == ("server", "read", "read foo.md")


def test_unknown_slash_is_forwarded_to_server():
    # let the server decide — never silently turn it into a model prompt
    assert classify("/frobnicate x")[0] == "server"


def test_slash_only_is_empty():
    assert classify("/")[0] == "empty"


def test_ink_client_offers_every_server_command():
    """Parity guard (ADR-0007): the Ink client's static command registry (clients/ink/src/commands.ts) must
    offer EVERY server command that commands.py advertises (SERVER_COMMANDS) — otherwise the TUI silently
    stops suggesting a real command (this is exactly how `/project` + `/switch` went un-offered). The TS file
    is a hand-maintained port of this SSOT; this asserts it never drifts again. Skips when clients/ink is
    absent (clean-room core-only export)."""
    from commands import SERVER_COMMANDS
    ts = Path(__file__).resolve().parents[3] / "clients" / "ink" / "src" / "commands.ts"
    if not ts.exists():
        pytest.skip("clients/ink not present (clean-room core-only)")
    ink_server = set(re.findall(r"name:\s*'([a-z0-9-]+)',\s*scope:\s*'server'", ts.read_text(encoding="utf-8")))
    missing = set(SERVER_COMMANDS) - ink_server
    assert not missing, f"clients/ink commands.ts is missing server commands from commands.py SERVER_COMMANDS: {sorted(missing)}"
