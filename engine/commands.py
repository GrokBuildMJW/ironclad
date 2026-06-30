"""Shared command routing for the thin clients (REPL + TUI).

One rule, used by both `client.py` and `tui.py` so they never drift:

  * input starting with ``/``  → a **command**
      - a small set of **local** commands (client/connection management) is handled
        on this side: ``/tasks /pending /coders /work /auto /health /help``
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
import shlex
import sys
from typing import List, Tuple


def build_agent_argv(template: str, *, bin: str, model: str, effort: str,
                     permission: str, prompt: str, feedback: str = "", mcp: str = "") -> List[str]:
    """Render a code-agent command template into an argv list (#449, C0R-9). ONE renderer shared by
    both launch paths — ``client._run_handover``/``default_cli_runner`` and ``gx10._do_launch`` —
    so neither re-implements template substitution. Lives here (stdlib-only: ``shlex``) rather than
    in ``providers.py`` so the zero-dependency headless client can import it without pulling pydantic.

    Tokens are split with shlex (POSIX), then placeholders are substituted **per token** so
    ``{prompt}`` (which contains spaces) stays exactly one argument. Unknown ``{x}`` are left as-is.
    ``{feedback}`` (#443) is the agent's deterministic result-capture path (e.g. Codex
    ``-o {feedback}``); a template that omits it (the Claude default) ignores it. ``{mcp}`` (#480) is a
    MULTI-token placeholder — it expands (via shlex) to 0+ args (the gated read-only Memory MCP config, or
    nothing when not under the sealed profile), so a template can carry it at the right position."""
    subs = {"bin": bin, "model": model, "effort": effort,
            "permission": permission, "prompt": prompt, "feedback": feedback}
    argv: List[str] = []
    for tok in shlex.split(template):
        if tok == "{mcp}":
            argv.extend(shlex.split(mcp or ""))        # #480: multi-token — empty mcp ⇒ no args
        elif tok.startswith("{") and tok.endswith("}") and tok[1:-1] in subs:
            argv.append(str(subs[tok[1:-1]]))          # whole token is a placeholder
        else:
            for k, v in subs.items():                  # placeholder(s) embedded in token
                tok = tok.replace("{" + k + "}", str(v))
            argv.append(tok)
    return argv


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
# DOCTOR (#503): /doctor is a LOCAL command (it GETs the gated /doctor endpoint and prints the report,
# mirroring /health) — NOT forwarded. Forwarding it made the server's _dispatch fall through to a billed
# model turn (no doctor branch there), while the real GET /doctor → _doctor_report had no in-product caller.
LOCAL_COMMANDS = {"tasks", "pending", "coders", "work", "auto", "health", "doctor", "help", "exit", "quit"}

#: Known orchestrator commands — forwarded to the server verbatim (minus the slash).
#: (Used for help + so `/help` can advertise them; unknown `/x` is still forwarded,
#: letting the server decide.)
SERVER_COMMANDS = {
    "status", "config", "clear", "read", "write", "cat", "ls",
    "watcher", "autopilot", "autoplan", "log-terminal", "initiative",
    "project", "switch", "prompts", "skills",
    "rag", "context", "tool", "generate",
}

HELP_TEXT = """\
Commands (with a / prefix) — plain text without / is sent to the orchestrator as a turn:

  local (client):
    /help              this help
    /tasks             TaskStore overview
    /pending           staged handovers for local code-agents
    /coders            which coding agents are bound/active (registry + boot probe) + providers
    /coders use <id>   pin all handovers to a coding agent at runtime (use auto to clear)
    /work              run all open handovers ONCE locally (in parallel)
    /auto on|off       background poller for handovers
    /health            server status
    /doctor            read-only preflight report (GET /doctor)
    exit               quit

  orchestrator (server):
    /status            status (model, perf, tasks, tools)
    /prompts           list the loaded prompt-library items (name, languages, description)
    /<prompt-name>     run a prompt item directly, e.g. /code-review diff="…" [--lang de]
    /skills            list the loaded skills (playbooks + typed tools, incl. MPR)
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
    /initiative new <name> --type mpr|software   create + activate an initiative
    /initiative list | use <slug> | active | reconcile [slug]
    /project list | new <name> [--type mpr|software] [--path <dir>] | active | track new|use|list
                   manage isolated projects (the guided setup command; /initiative is a deprecated alias)
    /switch <project_id>   rebind the engine to a project (own paths + memory partition)
    /generate <args>   scaffold a paved-road capability into the active project library
    /tool <name> <args>   run a tool directly/deterministically (no model election, no RAG)
    /rag on|off        toggle per-turn retrieval (RAG)
    /context           show the context-budget report
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
