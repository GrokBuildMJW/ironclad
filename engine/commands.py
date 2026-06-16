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

from typing import Tuple

#: Handled on the client side (connection / local code-agents).
LOCAL_COMMANDS = {"tasks", "pending", "work", "auto", "health", "help", "exit", "quit"}

#: Known orchestrator commands — forwarded to the server verbatim (minus the slash).
#: (Used for help + so `/help` can advertise them; unknown `/x` is still forwarded,
#: letting the server decide.)
SERVER_COMMANDS = {
    "status", "config", "clear", "read", "write", "cat", "ls",
    "watcher", "autopilot", "autoplan", "log-terminal",
}

HELP_TEXT = """\
Befehle (mit / prefix) — freier Text ohne / geht als Turn an den Orchestrator:

  lokal (Client):
    /help              diese Hilfe
    /tasks             TaskStore-Übersicht
    /pending           offene Handover für lokale code-agents
    /work              offene Handover EINMAL lokal abarbeiten (parallel)
    /auto on|off       Hintergrund-Poller für Handover
    /health            Server-Status
    exit               beenden

  Orchestrator (Server):
    /status            Status (Modell, Perf, Tasks, Tools)
    /config            aktive Konfiguration
    /clear             Kontext des Orchestrators leeren
    /read <pfad>       Datei im Server-Workdir lesen
    /ls [pfad]         Verzeichnis im Server-Workdir
    /watcher on|off    Auto-Advance (Reconciler)
    /autopilot on|off  Autopilot
    /autoplan on|off [N]
    (weitere: /write, /cat, /log-terminal)"""


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
