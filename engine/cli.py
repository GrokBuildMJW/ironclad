"""Ironclad CLI — the faithful Claude-Code-style client (framework-free).

> **LEGACY / DEPRECATED — superseded by the TypeScript terminal client in
> ``clients/ink/``,** which is the in-house reimplementation of this client's
> look-and-feel on a purpose-built renderer (the recommended interactive UI; see the
> top-level README / SETUP.md). Kept as the Python reference and still maintained, but no
> longer the primary client.

> **The terminal owns the mouse — selecting/copying works natively, no Shift.**
> Unlike a TUI framework (Textual/prompt_toolkit, which capture the mouse and force
> Shift to select), this client uses **Rich purely as a renderer**. Finished content
> is committed to native scrollback; only a small dynamic *tail* (spinner + a pinned
> status footer) is held in a Rich :class:`~rich.live.Live` region. No alt-screen, no
> mouse-tracking escapes (``?1049h`` / ``?100x`` / ``?1006``) are ever emitted, so the
> emulator keeps native drag-select, double/triple-click, wheel scroll and right-click
> / ``Ctrl+Shift+C`` copy with zero extra code.

Why **not** DECSTBM (the previous draft's scroll region): ConPTY persists the scroll
region across teardown (anthropics/claude-code#14716), Rich does not know about the
margin (long Markdown can overwrite the footer), and a background-thread footer redraw
races every Rich write at the escape level. The Rich ``Live`` approach is correct *by
construction*: ``Live.stop()`` clears only its own region, there is no margin to reset,
and every write goes through one lock. Tradeoff: the footer is pinned across **our**
writes (we control all of them — finished blocks are printed *above* the Live, the Live
redraws the tail); it is not immovable under raw ``\\n`` flooding from a child process,
but this CLI never pipes raw subprocess output into the transcript (code-agents run via
:func:`client.dispatch_pending`, logging through our ``log=`` sink). In practice the
footer stays put.

Reuses the existing seam unchanged: :class:`client.Server` (HTTP stream + the local
tool-bridge), :func:`commands.classify`, :func:`client.dispatch_pending`,
:func:`client._establish_session`, :class:`client.Tunnel`. Touches nothing server-side.
"""
from __future__ import annotations

import argparse
import atexit
import itertools
import json
import os
import random
import re
import sys
import threading
import time
import urllib.error
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Callable, Dict

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.spinner import SPINNERS, Spinner
from rich.text import Text
from rich.theme import Theme

import client
from client import Server
from commands import HELP_TEXT, classify, setup_output

# Raw single-key input so the prompt lives INSIDE a rounded box in the Live tail
# (like Claude Code) — keyboard only, NO mouse tracking, so native selection stays.
try:
    import msvcrt          # Windows
    _RAWKIND = "win"
except ImportError:
    try:
        import termios, tty  # POSIX
        _RAWKIND = "posix"
    except ImportError:
        _RAWKIND = None

# --------------------------------------------------------------------------- #
# Colour palette → Rich styles. Truecolor (Windows Terminal does ESC[38;2;…).
# --------------------------------------------------------------------------- #
ACCENT = "#d77757"        # claude brand terracotta — gutter, spinner, headings
ACCENT_HI = "#eb9f7f"     # spinner pulse highlight
TEXT = "#ffffff"          # primary body text
DIM = "#999999"           # chrome, hints, footer, placeholder
SUBTLE = "#666666"        # borders, separators
SUCCESS = "#4eba65"
ERROR = "#ff6b80"
WARNING = "#ffc107"
INPUT_TEAL = "#00a595"    # the prompt-glyph accent
MODEL_BLUE = "#7fb3d5"    # model name in the footer / welcome

IRON_THEME = Theme({
    "ironclad.accent": f"bold {ACCENT}",
    "ironclad.dim": DIM,
    "ironclad.subtle": SUBTLE,
    "ironclad.success": SUCCESS,
    "ironclad.error": ERROR,
    "ironclad.warning": WARNING,
    "ironclad.perf": DIM,
    "ironclad.model": MODEL_BLUE,
    # Markdown overrides → terracotta headings, dim rules, no heavy boxes.
    "markdown.h1": f"bold {ACCENT}",
    "markdown.h2": f"bold {ACCENT}",
    "markdown.h3": f"bold {TEXT}",
    "markdown.h4": f"bold {TEXT}",
    "markdown.item.bullet": ACCENT,
    "markdown.code": INPUT_TEAL,
    "markdown.block_quote": DIM,
    "rule.line": SUBTLE,
})

#: Rich is a RENDERER here, not a TUI: it never enables mouse tracking, so the terminal
#: keeps native selection/copy. We never set alt-screen / screen=True anywhere.
_console = Console(theme=IRON_THEME, highlight=False, soft_wrap=False, emoji=False,
                   safe_box=False)   # keep ROUNDED corners (╭╮╰╯) like Claude Code

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_LOCK = threading.RLock()       # serialises every Live refresh/start/stop + print-above

# Register the Claude-Code spinner frames so Rich's Spinner can use them by name.
SPIN_FRAMES = ["·", "✢", "✳", "✶", "✻", "✽"]
SPINNERS["ironclad"] = {"interval": 120, "frames": SPIN_FRAMES}
_spinner = Spinner("ironclad", style=ACCENT)

#: Cosmetic working verbs (picked once per turn).
VERBS = [
    "Befuddling", "Ruminating", "Pondering", "Schlepping", "Computing", "Brewing",
    "Combobulating", "Moonwalking", "Germinating", "Accomplishing", "Cogitating",
    "Conjuring", "Marinating", "Noodling", "Percolating", "Synthesizing", "Untangling",
    "Wrangling", "Forging", "Tinkering", "Distilling", "Crystallizing", "Orchestrating",
    "Deliberating", "Hammering", "Calibrating", "Assembling", "Sculpting", "Vibing",
]

_PLACEHOLDERS = itertools.cycle([
    'Try "edit engine/cli.py to add a flag"',
    'Try "/tasks" to see the TaskStore',
    'Try "summarise what changed in the last turn"',
    'Type a message · /help for commands · exit to quit',
    'Try "/work" to run pending handovers locally',
])

_STATUS: Dict[str, Any] = {
    "model": "—", "connected": False, "watcher": None, "autopilot": None,
    "pending": 0, "in_progress": 0, "done": 0, "perf": "", "tokens": 0,
    "thinking": False, "verb": "", "t0": 0.0, "phase": 0, "agent": "",
}

#: The single Live tail handle (set in repl()); the poller and _commit() refer to it.
_LIVE: Live | None = None

#: Live input-box state (the bordered prompt at the bottom, edited via raw keys).
_INPUT: Dict[str, Any] = {"buf": "", "active": False, "blink": True, "hint": ""}

#: Visual rows committed so far (scrollback). Used to pad the Live tail so the input
#: sits at the BOTTOM when content is short — Claude Code's gap-above-input, without a
#: scroll region (so native mouse selection stays intact).
_committed_rows = 0
_TAIL_ROWS = 5            # input box (3) + hint (1) + footer (1)


# --------------------------------------------------------------------------- #
# Tail renderables (spinner working-line + pinned footer).
# --------------------------------------------------------------------------- #
def _working_line() -> Text:
    """``✻ Verb… (12s · ↑ 2.3k tokens · ctrl-c to interrupt)`` — built fresh per refresh."""
    s = _STATUS
    phase = s["phase"] % len(SPIN_FRAMES)
    frame = SPIN_FRAMES[phase]
    pulse = ACCENT_HI if phase % 2 else ACCENT
    t = Text(no_wrap=True, overflow="ellipsis")
    t.append(f" {frame} ", style=pulse)
    t.append(f"{s['verb']}… ", style=ACCENT)
    elapsed = int(time.time() - s["t0"]) if s["t0"] else 0
    tok = s["tokens"]
    tok_s = f"{tok/1000:.1f}k" if tok >= 1000 else str(tok)
    meta = f"({elapsed}s · ↑ {tok_s} tokens · ctrl-c to interrupt)"
    t.append(meta, style=DIM)
    return t


def _footer() -> Text:
    """Single dim status line — the last row of the Live tail (pinned, no scroll region).
    Rich trims on the right (``overflow='ellipsis'``) to fit the terminal width."""
    s = _STATUS
    t = Text(no_wrap=True, overflow="ellipsis")
    t.append(" ◆ Ironclad", style=f"bold {ACCENT}")
    t.append("  ·  ", style=SUBTLE)
    t.append("model ", style=DIM)
    t.append(str(s["model"]), style=MODEL_BLUE)
    t.append("  ·  ", style=SUBTLE)
    t.append("●" if s["connected"] else "○", style=SUCCESS if s["connected"] else ERROR)
    t.append(" conn", style=DIM)
    t.append("  ·  ", style=SUBTLE)
    t.append("●" if s["watcher"] else "○", style=SUCCESS if s["watcher"] else DIM)
    t.append(" watch ", style=DIM)
    t.append("●" if s["autopilot"] else "○", style=SUCCESS if s["autopilot"] else DIM)
    t.append(" auto", style=DIM)
    t.append("  ·  ", style=SUBTLE)
    t.append(f"{s['pending']}P/{s['in_progress']}IP/{s['done']}D", style=DIM)
    if s.get("agent"):                                    # #453: which coder was last routed
        t.append("  ·  ", style=SUBTLE)
        t.append("coder ", style=DIM)
        t.append(str(s["agent"]), style=MODEL_BLUE)
    if s["perf"]:
        t.append("  ·  ", style=SUBTLE)
        t.append(s["perf"], style="ironclad.perf")
    else:
        t.append("  ·  ", style=SUBTLE)
        t.append("? for shortcuts", style=DIM)
    return t


def _input_box():
    """The prompt affordance — top rule, ``> `` prompt line, bottom rule (Claude-Code
    style; open sides, not a closed box). Edited via raw keys (no mouse capture)."""
    buf = _INPUT["buf"]
    caret = "▏" if _INPUT["blink"] else " "
    line = Text(no_wrap=True, overflow="ellipsis")
    line.append("> ", style=f"bold {INPUT_TEAL}")
    if buf:
        line.append(buf, style=TEXT)
        line.append(caret, style=ACCENT)
    else:
        line.append(caret, style=ACCENT)
        line.append(_INPUT["hint"] or "Frag etwas …", style=SUBTLE)
    return Group(Rule(style=SUBTLE), line, Rule(style=SUBTLE))


def _hint_line() -> Text:
    """A single dim hint row beneath the input rules (Claude-Code keeps this minimal)."""
    t = Text(no_wrap=True, overflow="ellipsis", style=DIM)
    t.append("  /help", style=ACCENT)
    t.append(" · ", style=SUBTLE)
    t.append("exit", style=ACCENT)
    t.append(" · Maus markiert/kopiert nativ", style=DIM)
    return t


def _tail():
    """The dynamic region the Live owns: input box (idle), spinner (thinking), always the
    footer. When idle, the input is pushed to the BOTTOM of the screen with blank padding
    (Claude-Code's gap-above-input) — computed from committed rows, no scroll region."""
    if _STATUS["thinking"]:
        return Group(_working_line(), _footer())
    if _INPUT["active"]:
        tail = (_input_box(), _hint_line(), _footer())
        pad = max(0, _console.size.height - _committed_rows - _TAIL_ROWS)
        if pad:
            return Group(*([Text("")] * pad), *tail)
        return Group(*tail)
    return _footer()


def _refresh() -> None:
    """Thread-safe Live refresh (Rich's Live is not thread-safe on its own)."""
    if _LIVE is None:
        return
    with _LOCK:
        try:
            _LIVE.refresh()
        except Exception:  # noqa: BLE001 — never let a refresh race crash a thread
            pass


def _commit(renderable) -> None:
    """Print a finished renderable ABOVE the Live tail → it scrolls into native
    scrollback. Rich routes ``console.print`` above an active Live automatically.
    Track the rendered height so _tail() can pad the input to the screen bottom."""
    global _committed_rows
    with _LOCK:
        try:
            _committed_rows += len(_console.render_lines(renderable, pad=False))
        except Exception:  # noqa: BLE001 — height is only for cosmetic padding
            _committed_rows += 1
        _console.print(renderable)


# --------------------------------------------------------------------------- #
# Background poller — one daemon thread refreshes status + animates the spinner.
# (Per the *no-polling* memory note: that rule is about not polling background-task
# *completion*; a status-footer refresh loop is the intended pinned-footer mechanism
# and is required by the spec, so it stays.)
# --------------------------------------------------------------------------- #
def _poller(srv: Server, stop: threading.Event) -> None:
    tick = 0
    while not stop.wait(0.12):                  # ~8 Hz spinner cadence
        if tick % 17 == 0:                      # ~every 2s: poll health + tasks
            try:
                h = srv.health()
                _STATUS.update(connected=True, model=h.get("model", "—"),
                               watcher=h.get("watcher"), autopilot=h.get("autopilot"))
                c = Counter(t.get("status") for t in srv.tasks())
                _STATUS.update(pending=c.get("pending", 0),
                               in_progress=c.get("in_progress", 0),
                               done=c.get("done", 0))
            except Exception:  # noqa: BLE001 — unreachable → show disconnected, keep going
                _STATUS["connected"] = False
        if _STATUS["thinking"]:
            _STATUS["phase"] += 1
        if tick % 4 == 0:                       # ~0.5s caret blink in the input box
            _INPUT["blink"] = not _INPUT["blink"]
        tick += 1
        _refresh()


# --------------------------------------------------------------------------- #
# Streamed-text filter: [perf] → status, role labels/DONE-banner dropped, blanks
# collapsed; the filtered prose is rendered as ONE Markdown block on completion.
# --------------------------------------------------------------------------- #
_TOK_RE = re.compile(r"(\d+)\s*tok")


def _stream_turn(srv: Server, payload: str) -> None:
    _STATUS.update(thinking=True, verb=random.choice(VERBS), t0=time.time(),
                   tokens=0, phase=0, agent="")           # #453: clear last turn's coder (no stale "live")
    _refresh()
    answer_lines: list[str] = []
    buf = {"s": "", "blank": True}

    def route(line: str) -> None:
        clean = _ANSI_RE.sub("", line)
        st = clean.strip()
        # 1) [perf] → status display, NOT the chat.
        i = st.find("[perf]")
        if i != -1:
            perf = st[i + len("[perf]"):].strip()
            _STATUS["perf"] = perf
            m = _TOK_RE.search(perf)
            if m:
                _STATUS["tokens"] = int(m.group(1))
            return
        # 1b) [agent] → which coder is being called (status footer, NOT the chat). #453
        j = st.find("[agent]")
        if j != -1:
            _STATUS["agent"] = st[j + len("[agent]"):].strip()
            return
        # 2) "======== ✓ DONE … ========" banner → drop.
        if "===" in st and ("DONE" in st or "✓" in st):
            return
        # 3) role labels ([GX10] / [Qwen (planning)] / [… planning …]) → drop.
        if (st == "[GX10]"
                or (st.startswith("[Qwen") and st.endswith("]"))
                or (st.startswith("[") and "planning" in st and st.endswith("]"))):
            return
        # 4) collapse runs of blank lines (keep one for Markdown paragraph spacing).
        if not st:
            if buf["blank"]:
                return
            buf["blank"] = True
            answer_lines.append("")
            return
        buf["blank"] = False
        answer_lines.append(line)

    def on_text(t: str) -> None:
        buf["s"] += t
        while "\n" in buf["s"]:
            ln, buf["s"] = buf["s"].split("\n", 1)
            route(ln)

    try:
        # X-Local-Tools:1 is set by client.Server → passed-through code-tools run
        # LOCALLY here via the tool-bridge, against this machine's working copy.
        srv.chat_stream(payload, on_text)
    except KeyboardInterrupt:
        try:
            srv.cancel()
        except Exception:  # noqa: BLE001
            pass
        _commit(Text("  ⨯ interrupted", style=WARNING))
    except urllib.error.URLError as e:
        _commit(Text(f"  ✗ /chat/stream failed: {e}", style=ERROR))
    except Exception as e:  # noqa: BLE001 — never crash the REPL on a stream hiccup
        _commit(Text(f"  ✗ /chat/stream failed: {e!r}", style=ERROR))
    finally:
        if buf["s"]:
            route(buf["s"])
        _STATUS["thinking"] = False
        body = "\n".join(answer_lines).strip("\n")
        if body:
            from rich.padding import Padding
            # Assistant answer: Markdown, indented 2 cols so it reads distinct from the
            # dim "> you" echo above it (cleaner than a standalone gutter glyph).
            _commit(Padding(Markdown(body, code_theme="monokai"), (0, 0, 0, 2)))
        _refresh()                                          # footer back to idle


# --------------------------------------------------------------------------- #
# Local commands (/help /tasks /pending /work /auto /health) — emitted ABOVE Live.
# --------------------------------------------------------------------------- #
def _handle_local(srv: Server, codedir: Path, pool: ThreadPoolExecutor, claimed: set,
                  max_agents: int, auto: Dict[str, Any], name: str, payload: str) -> None:
    try:
        if name == "help":
            _commit(Text(HELP_TEXT, style=TEXT))
        elif name == "health":
            _commit(Text("  " + json.dumps(srv.health(), ensure_ascii=False), style=DIM))
        elif name == "tasks":
            ts = srv.tasks()
            if not ts:
                _commit(Text("  (no tasks)", style=DIM))
            for t in ts:
                _commit(Text(
                    f"  {t.get('status','?'):11} {t.get('id','?'):10} "
                    f"{t.get('type','?'):14} {t.get('title','')}", style=TEXT))
        elif name == "pending":
            ps = srv.pending()
            if not ps:
                _commit(Text("  (no open handovers)", style=DIM))
            for it in ps:
                _commit(Text(
                    f"  {it.get('id','?'):10} {it.get('agent','?'):7} "
                    f"{it.get('type','?'):14} {it.get('title','')}", style=TEXT))
        elif name == "coders":
            parts = payload.split()
            if len(parts) >= 2 and parts[1].lower() == "use":  # #454: /coders use <id>|auto
                res = srv.set_coder_pin(parts[2] if len(parts) >= 3 else "auto")
                pin = res.get("pinned")
                _commit(Text(f"  → pinned coder: {pin}" if pin
                             else "  → coder pin cleared (auto: the staged agent per task)", style="cyan"))
            data = srv.coders()                            # #452: which coding agents are bound + providers
            coding = data.get("coding_agents") or []
            pinned = data.get("pinned")
            _commit(Text(f"  pinned: {pinned}  (/coders use auto to clear)" if pinned
                         else "  routing: auto (orchestrator's staged agent per task)", style=DIM))
            if not coding:
                _commit(Text("  (no coding agents configured)", style=DIM))
            for a in coding:
                enabled = a.get("enabled", True)            # #460: False ⇒ onboarded but not yet activated
                mark = "◌" if not enabled else ("●" if a.get("bound") else "○")
                is_pin = pinned and str(a.get("id", "")).upper() == str(pinned).upper()
                if not enabled:
                    suffix = "  (onboarded · disabled)"
                elif is_pin:
                    suffix = "  ← pinned"
                else:
                    suffix = "" if a.get("bound") else "  (binary not found)"
                _commit(Text(f"  {mark} {str(a.get('id','?')):8} {a.get('model','—')}" + suffix, style=TEXT))
            prov = (data.get("providers") or {})
            pool = prov.get("pool") or []
            if pool:
                b = prov.get("budget") or {}
                _commit(Text(f"  providers (fan-out): "
                             f"{'active' if prov.get('active') else 'inactive'} · "
                             f"spent ${b.get('spent_usd', 0):.4f}", style=DIM))
                for p in pool:
                    mark = "●" if p.get("reachable") else "○"
                    tail = f"  ← {p.get('last_route_reason')}" if p.get("last_route_reason") else ""
                    _commit(Text(f"    {mark} {str(p.get('id','?')):14} "
                                 f"{str(p.get('kind','?')):9} {p.get('model','—')}{tail}", style=TEXT))
        elif name == "work":
            log: Callable[[str], None] = lambda s: _commit(Text(s, style=DIM))
            futs = client.dispatch_pending(srv, codedir, pool, claimed, log=log)
            if not futs:
                _commit(Text("  (no new handovers)", style=DIM))
            else:
                _commit(Text(
                    f"  → {len(futs)} handover(s) started (≤{max_agents} parallel), waiting …",
                    style=ACCENT))
                done_set, _ = wait(futs)
                ok = sum(1 for f in done_set if f.result() is True)
                _commit(Text(f"  done: {ok}/{len(futs)} cleanly uploaded", style=SUCCESS))
        elif name == "auto":
            _handle_auto(srv, codedir, pool, claimed, max_agents, auto, payload)
    except urllib.error.HTTPError as e:                     # #454: show the server's JSON error detail
        _commit(Text(f"  ✗ {client.http_error_msg(e)}", style=ERROR))
    except urllib.error.URLError as e:
        _commit(Text(f"  ✗ {e}", style=ERROR))
    except Exception as e:  # noqa: BLE001
        _commit(Text(f"  ✗ {e!r}", style=ERROR))


def _handle_auto(srv: Server, codedir: Path, pool: ThreadPoolExecutor, claimed: set,
                 max_agents: int, auto: Dict[str, Any], payload: str) -> None:
    """``/auto on|off`` — background handover poller (reuses dispatch_pending), wired
    the same way as client.repl's _auto_loop."""
    parts = payload.split()
    arg = parts[1].lower() if len(parts) > 1 else ""

    def _auto_loop(stop: threading.Event) -> None:
        while not stop.wait(5.0):
            try:
                client.dispatch_pending(srv, codedir, pool, claimed,
                                        log=lambda s: _commit(Text(s, style=DIM)))
            except Exception as e:  # noqa: BLE001
                _commit(Text(f"  ✗ auto-poll: {e!r}", style=ERROR))

    if arg == "on":
        if auto.get("stop") is None:
            stop = threading.Event()
            auto["stop"] = stop
            threading.Thread(target=_auto_loop, args=(stop,), daemon=True).start()
            _commit(Text(
                f"  [AUTO] poller ON — pulls handovers every 5s, ≤{max_agents} parallel",
                style=ACCENT))
        else:
            _commit(Text("  [AUTO] already running", style=DIM))
    elif arg == "off":
        if auto.get("stop") is not None:
            auto["stop"].set()
            auto["stop"] = None
            _commit(Text("  [AUTO] poller OFF", style=DIM))
        else:
            _commit(Text("  [AUTO] was not active", style=DIM))
    else:
        state = "ON" if auto.get("stop") else "OFF"
        _commit(Text(f"  [AUTO] {state}  |  /auto on / /auto off", style=DIM))


# --------------------------------------------------------------------------- #
# Welcome panel (committed once, before the Live starts → scrolls into scrollback).
# --------------------------------------------------------------------------- #
def _print_welcome(srv: Server, codedir: Path, max_agents: int) -> None:
    try:
        h = srv.health()
        reachable = True
    except Exception:  # noqa: BLE001
        h, reachable = {}, False
        _STATUS["connected"] = False
    if reachable:
        _STATUS.update(connected=True, model=h.get("model", "—"),
                       watcher=h.get("watcher"), autopilot=h.get("autopilot"))
    dot = Text("●", style=SUCCESS) if reachable else Text("○", style=ERROR)
    model = str(h.get("model", "—"))
    # Borderless header (Claude-Code style): a small block mark + three info lines.
    _commit(Text.assemble(("▐▛██▜▌ ", f"bold {ACCENT}"), ("Ironclad", f"bold {TEXT}"),
                          ("  ·  Orchestrator-Client", DIM)))
    _commit(Text.assemble(("▝▜██▛▘ ", f"bold {ACCENT}"), (model, MODEL_BLUE),
                          ("  ·  ", SUBTLE), dot, (" ", ""), (srv.base, DIM)))
    _commit(Text.assemble(("  ▘▘   ", f"bold {ACCENT}"), (str(codedir), DIM),
                          (f"  ·  ≤{max_agents} agents", DIM)))
    _commit(Text(""))
    if not reachable:
        _commit(Text("  ⚠ server unreachable — retrying in the background",
                     style=WARNING))


# --------------------------------------------------------------------------- #
# Input affordance — a dim rotating hint line + a coloured glyph prompt. We drop the
# literal rounded *input box* (impossible to fill with native input() line editing
# without a banned mouse-capturing TUI) in favour of the rounded WELCOME box + glyph.
# --------------------------------------------------------------------------- #
def _read_turn() -> str:
    """Read one line of input. When raw-key support exists, edit it live INSIDE the
    rounded input box in the Live tail (no mouse capture → native selection stays).
    Otherwise fall back to a plain ``input()`` prompt (Live paused, no box)."""
    if _RAWKIND is None:                                  # no raw keyboard → plain prompt
        with _LOCK:
            if _LIVE is not None:
                _LIVE.stop()
        try:
            return input("\001\x1b[1m\x1b[38;2;0;165;149m\002❯ \001\x1b[0m\002")
        finally:
            with _LOCK:
                if _LIVE is not None:
                    _LIVE.start(refresh=False)
            _refresh()
    _INPUT.update(buf="", active=True, hint=next(_PLACEHOLDERS), blink=True)
    _refresh()
    try:
        return _raw_loop()
    finally:
        _INPUT["active"] = False
        _refresh()


def _getch():
    """Read one keypress (wide char). Windows: msvcrt; POSIX raw is set in _raw_loop."""
    if _RAWKIND == "win":
        return msvcrt.getwch()
    return sys.stdin.read(1)


def _raw_loop() -> str:
    """Edit ``_INPUT['buf']`` keystroke-by-keystroke until Enter; render via the Live."""
    old = None
    if _RAWKIND == "posix":
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        tty.setcbreak(fd)
    try:
        while True:
            ch = _getch()
            if ch in ("", None):                         # EOF
                raise EOFError
            if ch in ("\r", "\n"):                        # Enter → submit
                return _INPUT["buf"]
            if ch == "\x03":                              # Ctrl+C
                raise KeyboardInterrupt
            if ch in ("\x08", "\x7f"):                    # Backspace
                _INPUT["buf"] = _INPUT["buf"][:-1]
            elif ch == "\x15":                            # Ctrl+U → clear line
                _INPUT["buf"] = ""
            elif ch in ("\x00", "\xe0"):                  # Windows special-key prefix → skip next
                _getch()
            elif ch == "\x1b":                            # POSIX arrow/ESC seq → drop the rest
                if _RAWKIND == "posix":
                    _getch(); _getch()
            elif ch >= " ":                               # printable (incl. pasted text)
                _INPUT["buf"] += ch
            _refresh()
    finally:
        if old is not None:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old)


# --------------------------------------------------------------------------- #
# REPL.
# --------------------------------------------------------------------------- #
def repl(srv: Server, codedir: Path, max_agents: int = client.DEFAULT_MAX_AGENTS) -> None:
    global _LIVE
    claimed: set = set()
    auto: Dict[str, Any] = {"stop": None}
    pool = ThreadPoolExecutor(max_workers=max_agents, thread_name_prefix="codeagent")
    stop = threading.Event()

    _print_welcome(srv, codedir, max_agents)        # committed BEFORE the Live starts

    # auto_refresh=False → only our poller thread refreshes; no Rich background thread
    # to race us. get_renderable=_tail → a fresh tail is built on every refresh().
    _LIVE = Live(console=_console, get_renderable=_tail, auto_refresh=False,
                 transient=False, refresh_per_second=8)
    with _LOCK:
        _LIVE.start(refresh=True)
    threading.Thread(target=_poller, args=(srv, stop), daemon=True).start()

    try:
        while True:
            # The input box lives INSIDE the Live tail (raw-key edit), so the Live stays
            # up here. The plain-input() fallback (no raw keyboard) pauses it itself.
            try:
                line = _read_turn()
            except (EOFError, KeyboardInterrupt):
                _console.print()
                break

            kind, name, payload = classify(line)
            if kind == "empty":
                continue
            if kind == "local" and name in ("exit", "quit"):
                break
            if kind == "local":
                _commit(Text(f"> {payload}", style=DIM))    # echo the submitted command
                _handle_local(srv, codedir, pool, claimed, max_agents, auto, name, payload)
                continue
            # kind in ("server", "turn") → orchestrator. classify() already stripped the
            # leading "/" for server commands, so both share the stream path.
            _commit(Text(f"> {payload}", style=DIM))        # user echo, dim gutter
            try:
                _stream_turn(srv, payload)
            except KeyboardInterrupt:
                # Ctrl+C between turns / during the gutter print → ignore, stay in REPL.
                _STATUS["thinking"] = False
                _refresh()
    finally:
        stop.set()
        if auto.get("stop") is not None:
            auto["stop"].set()
        with _LOCK:
            try:
                _LIVE.stop()                         # clears the live region cleanly
            except Exception:  # noqa: BLE001
                pass
        _LIVE = None
        pool.shutdown(wait=False, cancel_futures=True)
        _console.show_cursor(True)
        _console.print()                             # land on a fresh line


# --------------------------------------------------------------------------- #
# Entry point — Tunnel/session via client helpers, identical wiring to client.main().
# --------------------------------------------------------------------------- #
def main() -> None:
    p = argparse.ArgumentParser(
        description="Ironclad CLI — Claude-Code-style client (pinned footer, native mouse)")
    p.add_argument("--server", default=client.DEFAULT_SERVER,
                   help=f"Orchestrator server URL (default {client.DEFAULT_SERVER})")
    p.add_argument("--codedir", default=".",
                   help="Local code root (cwd for passed-through code-tools / claude --print)")
    p.add_argument("--max-agents", type=int, default=client.DEFAULT_MAX_AGENTS,
                   help=f"Max parallel local code-agents (default {client.DEFAULT_MAX_AGENTS})")
    args = p.parse_args()

    setup_output()                                   # UTF-8-safe stdout + VT on Windows
    # Belt-and-suspenders: no scroll region is ever set, so nothing to reset — but always
    # restore the cursor on the way out, whatever path we exit by.
    atexit.register(lambda: _console.show_cursor(True))

    srv = Server(args.server, token=client.SERVER_TOKEN)
    codedir = Path(args.codedir).expanduser().resolve()
    os.chdir(codedir)        # passed-through code-tools (run_tool) act on YOUR local tree

    # Phase d: open the transport first (sealed profile), then the session handshake.
    transport = (client.Tunnel(client.TUNNEL_CMD, args.server)
                 if client.TUNNEL_CMD else client._NullCtx())
    with transport:
        stop_hb, _ = client._establish_session(srv)
        try:
            repl(srv, codedir, max_agents=args.max_agents)
        finally:
            if stop_hb is not None:
                stop_hb.set()
            srv.session_close()


if __name__ == "__main__":
    main()
