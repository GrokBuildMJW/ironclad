"""Shared command routing for the thin clients (REPL + TUI).

One rule, used by both `client.py` and `tui.py` so they never drift:

  * input starting with ``/``  → a **command**
      - a small set of **local** commands (client/connection management) is handled
        on this side: ``/tasks /pending /work /auto /health /help``
      - everything else is **forwarded to the orchestrator** (the leading ``/`` is
        stripped) so the server's own dispatcher handles it exactly like the old CLI:
        ``/status /config /clear /read /write /cat /ls /watcher /autopilot
        /autoplan /log-terminal`` …
  * a bare ``exit`` / ``quit`` → leave
  * anything else → a normal **turn** sent to the model

This restores the full pre-split command set (which used to be typed without a slash
in the monolithic CLI) under a single, predictable ``/command`` convention.
"""
from __future__ import annotations

import os
import sys
from typing import Tuple


def setup_output() -> None:
    """UTF-8-safe stdout/stderr + ANSI enable on Windows — shared by the REPL and TUI so a
    non-ASCII byte never raises the cp1252 ``UnicodeEncodeError`` class. Failure-tolerant."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # py3.7+ text streams
        except (AttributeError, ValueError):
            pass
    if os.name == "nt":
        os.system("")  # enable ANSI/VT processing in legacy Windows consoles


#: Handled on the client side (connection / local code-agents).
LOCAL_COMMANDS = {"tasks", "pending", "work", "auto", "health", "help", "exit", "quit"}

#: Known orchestrator commands — forwarded to the server verbatim (minus the slash).
#: (Used for help + so `/help` can advertise them; unknown `/x` is still forwarded,
#: letting the server decide.)
SERVER_COMMANDS = {
    "status", "config", "clear", "read", "write", "cat", "ls",
    "watcher", "autopilot", "autoplan", "log-terminal", "vorhaben",
}

HELP_TEXT = """\
Commands (with a / prefix) — plain text without / is sent to the orchestrator as a turn:

  local (client):
    /help              this help
    /tasks             TaskStore overview
    /pending           staged handovers for local code-agents
    /work              run all open handovers ONCE locally (in parallel)
    /auto on|off       background poller for handovers
    /health            server status
    exit               quit

  orchestrator (server):
    /status            status (model, perf, tasks, tools)
    /config            active configuration
    /config get <key>          read a dotted config key (e.g. mpr.enabled)
    /config set <key> <value>  override a config key at runtime
                               (on|off|true|false|num|str)
    /clear             clear the orchestrator's context
    /read <path>       read a file in the server workdir
    /ls [path]         list a directory in the server workdir
    /watcher on|off    auto-advance (reconciler)
    /autopilot on|off  autopilot
    /autoplan on|off [N]
    /vorhaben new <name> --typ mpr|software   create + activate a vorhaben
    /vorhaben list | use <slug> | active | reconcile [slug]
    (more: /write, /cat, /log-terminal)"""


def classify(line: str) -> Tuple[str, str, str]:
    """Classify one input line.

    Returns ``(kind, name, payload)`` where *kind* is:
      - ``"empty"``  — nothing to do
      - ``"turn"``   — *payload* is the prompt to send to the model
      - ``"local"``  — *name* is a local command, *payload* the full command line
      - ``"server"`` — *payload* is the command line to forward (slash already stripped)
    """
    s = line.strip()
    if not s:
        return ("empty", "", "")
    if s.lower() in ("exit", "quit"):
        return ("local", "exit", s.lower())
    if not s.startswith("/"):
        return ("turn", "", s)
    body = s[1:].strip()
    if not body:
        return ("empty", "", "")
    name = body.split()[0].lower()
    if name in LOCAL_COMMANDS:
        return ("local", name, body)
    return ("server", name, body)
