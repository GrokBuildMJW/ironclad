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
    assert classify("/design --options 2") == ("server", "design", "design --options 2")


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


# ── #934: alias / unambiguous-prefix / did-you-mean in classify ──────────────────────────────────────
def test_classify_alias_expands_to_canonical():
    assert classify("/lg --tree X") == ("server", "lifecycle", "lifecycle gate --tree X")


def test_classify_typo_suggests_and_is_not_forwarded():
    kind, name, _ = classify("/confog rag on")
    assert kind == "suggest" and name == "config"     # did-you-mean, never sent (no billed turn)


def test_classify_prompt_name_still_forwards():
    # a bare /<prompt-name> (not close to any command) must reach the server's prompt resolver, not 'suggest'
    assert classify("/code-review diff=x")[0] == "server"


def test_classify_destructive_prefix_suggests_not_auto():
    kind, name, _ = classify("/proj list")             # 'proj' → project, but project is destructive
    assert kind == "suggest" and name == "project"


def test_classify_known_verb_forwards_verbatim():
    assert classify("/config get mpr.enabled") == ("server", "config", "config get mpr.enabled")


def test_classify_local_and_turn_unchanged():
    assert classify("/help")[0] == "local" and classify("hello")[0] == "turn"


def test_guidance_recommends_project_not_the_deprecated_initiative():
    # #964 regression: the model system prompt + client HELP_TEXT + the fail-closed messages must teach
    # /project (the primary/guided command), NOT the deprecated /initiative alias, for creating a project.
    # (The alias's own `_initiative_command` help legitimately keeps /initiative; this guards the GUIDANCE.)
    from commands import HELP_TEXT
    assert "/project new" in HELP_TEXT and "/initiative new" not in HELP_TEXT
    import importlib
    m = importlib.import_module("messages")
    for lang in ("en", "de"):
        msg = m.msg("init.no_active", lang=lang)
        assert "/project new" in msg and "/initiative new" not in msg, f"{lang} init.no_active still teaches /initiative"
    from pathlib import Path
    import gx10
    prompt = (Path(gx10.__file__).parent / "prompts" / "GX10_Orchestrator_SystemPrompt.md").read_text(encoding="utf-8")
    assert "/project new" in prompt and "/project active" in prompt
    assert "`/initiative new" not in prompt and "`/initiative active" not in prompt   # no deprecated-alias advice


def test_orchestrator_system_prompt_is_english():
    # #966: the exported base system prompt must be English (English-only export). The German rendering
    # lives as a private, non-exported override (deploy/prompts/, selected via GX10_PROMPT).
    from pathlib import Path
    import gx10
    p = (Path(gx10.__file__).parent / "prompts" / "GX10_Orchestrator_SystemPrompt.md").read_text(encoding="utf-8")
    assert "You are the **ironclad Orchestrator**" in p            # English intro
    assert "Du bist der" not in p and "Nicht erfinden" not in p    # no German prose
    import re
    assert not re.search(r"[äöüÄÖÜß]", p), "the base prompt must carry no German umlauts"
