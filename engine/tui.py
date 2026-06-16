"""Full-screen TUI client — the old GX10 look-and-feel over the server/client split.

> **Same screen as the monolithic CLI, but the brain is remote.** This reuses the
> orchestrator's own prompt_toolkit primitives (the scrolling output pane, the input
> line, the colour helpers) from :mod:`gx10`, and drives them from the HTTP server
> instead of a local agent. Turns are **streamed** live (token by token, via
> ``/chat/stream``); a persistent bottom toolbar shows remote status (model, last
> perf, task counts, watcher/autopilot, connection) refreshed in the background.

Layout (identical structure to the old CLI, so ``gx10._get_output``'s 6-row bottom
budget fits): output pane · ─ · input · ─ · toolbar(3).

Dependencies: prompt_toolkit (the UI lib). If it is missing, ``main()`` falls back to
the plain line REPL in :mod:`client`. Importing :mod:`gx10` here does NOT require
openai (its import is soft) — only the UI primitives are used; the agent lives on the
server.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Queue
from typing import Any, Dict

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

try:
    from prompt_toolkit import Application
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.formatted_text import ANSI
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.keys import Keys
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import HSplit, Window
    from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
    HAS_PT = True
except ImportError:
    HAS_PT = False

import gx10  # UI primitives (importable without openai)
import client
from client import Server
from commands import HELP_TEXT, classify

#: Output scrollback offset in visual rows from the bottom (0 = follow newest).
_SCROLL: Dict[str, int] = {"rows": 0}
#: How many trailing lines scrollback can reach (bounds per-render cost).
_MAX_SCROLL_LINES = 3000
#: Multi-line pastes, stored and shown compressed as "[Pasted #N +L lines]".
_PASTES: list = []
_PASTE_RE = re.compile(r"\[Pasted #(\d+) \+\d+ lines\]")

#: Shared status for the toolbar, refreshed by the background poller + stream parse.
_STATUS: Dict[str, Any] = {
    "model": "?", "server": "", "codedir": "",
    "watcher": None, "autopilot": None,
    "pending": 0, "in_progress": 0, "done": 0,
    "perf": "", "connected": False,
}


# --------------------------------------------------------------------------- #
# Toolbar — old GX10 branding bar, fed with remote status.
# --------------------------------------------------------------------------- #
def _toolbar():
    st = gx10._status
    frame = gx10.SPINNER_FRAMES[int(time.time() * 8) % len(gx10.SPINNER_FRAMES)]
    conn_color = "fg:ansigreen bold" if _STATUS["connected"] else "fg:ansired bold"
    w_color = "fg:ansigreen bold" if _STATUS["watcher"] else "fg:ansigray"
    a_color = "fg:ansigreen bold" if _STATUS["autopilot"] else "fg:ansigray"
    conn_dot = "●" if _STATUS["connected"] else "○"
    w_dot = "●" if _STATUS["watcher"] else "○"
    a_dot = "●" if _STATUS["autopilot"] else "○"
    if st["thinking"]:
        mid = f"  {frame}  {st['label']}...   Ctrl+C = cancel   "
    elif _SCROLL["rows"] > 0:
        mid = f"  ↑ history (+{_SCROLL['rows']})  ·  PageUp/PageDown  ·  PageDown→bottom   "
    else:
        mid = "  Orchestrator client  ·  streaming   |   /help · exit · PageUp=history   "
    line3 = (f"     {_STATUS['model']}  ·  {_STATUS['perf'] or '—'}  ·  "
             f"tasks {_STATUS['pending']}P/{_STATUS['in_progress']}IP/{_STATUS['done']}D"
             f"  ·  {_STATUS['server']}")
    return [
        ("fg:ansiblue bold", " ██ "),
        ("bold", "Ironclad"),
        ("", "  powered by "),
        ("fg:ansiblue bold", "MJWC-AI-LAB"),
        ("", "\n"),
        ("fg:ansiblue bold", " ██ "),
        ("", mid),
        (conn_color, conn_dot), ("", " Server  "),
        (w_color, w_dot), ("", " Watcher  "),
        (a_color, a_dot), ("", " Autopilot\n"),
        ("fg:ansigray", line3),
    ]


# --------------------------------------------------------------------------- #
# Background: poll the server for toolbar status.
# --------------------------------------------------------------------------- #
def _poller(srv: Server, stop: threading.Event) -> None:
    while not stop.wait(3.0):
        try:
            h = srv.health()
            _STATUS.update(connected=True, model=h.get("model", "?"),
                           watcher=h.get("watcher"), autopilot=h.get("autopilot"))
            counts = Counter(t.get("status") for t in srv.tasks())
            _STATUS.update(pending=counts.get("pending", 0),
                           in_progress=counts.get("in_progress", 0),
                           done=counts.get("done", 0))
        except Exception:  # noqa: BLE001 — network gone -> red, keep polling
            _STATUS["connected"] = False
        if gx10._UI_APP is not None:
            gx10._UI_APP.invalidate()


# --------------------------------------------------------------------------- #
# Worker: consume input, stream turns from the server, run local code-agents.
# --------------------------------------------------------------------------- #
def _worker(srv: Server, codedir: Path, q: "Queue[str]", app: "Application",
            pool: ThreadPoolExecutor, claimed: set, auto: Dict[str, Any]) -> None:
    log = gx10._ui_print

    def _stream(payload: str) -> None:
        gx10._status["thinking"] = True
        gx10._status["label"] = "Qwen"
        # Buffer line by line so we can filter out the [perf] line: it belongs ONLY
        # in the toolbar (bottom), not in the chat history.
        partial = {"buf": ""}

        def _emit_line(line: str) -> None:
            clean = gx10._ANSI_LEN_RE.sub("", line)
            idx = clean.find("[perf]")
            if idx != -1:
                _STATUS["perf"] = clean[idx:].strip()   # toolbar only
                return
            gx10._ui_print(line)                          # with newline into the pane

        def _on_text(t: str) -> None:
            partial["buf"] += t
            while "\n" in partial["buf"]:
                line, partial["buf"] = partial["buf"].split("\n", 1)
                _emit_line(line)

        try:
            srv.chat_stream(payload, _on_text)
        except Exception as e:  # noqa: BLE001
            gx10._ui_print(gx10.col(f"  ✗ /chat/stream failed: {e!r}", gx10.C.RED))
        finally:
            if partial["buf"]:
                _emit_line(partial["buf"])
            gx10._status["thinking"] = False
            if gx10._UI_APP is not None:
                gx10._UI_APP.invalidate()

    while True:
        text = q.get()
        if text == "\x04":
            app.exit(); return
        kind, name, payload = classify(text)
        if kind == "empty":
            continue
        if kind == "local" and name in ("exit", "quit"):
            app.exit(); return
        if kind == "local":
            if name == "help":
                log(HELP_TEXT)
            elif name == "health":
                try:
                    log(gx10.col("  " + json.dumps(srv.health(), ensure_ascii=False), gx10.C.GRAY))
                except Exception as e:  # noqa: BLE001
                    log(gx10.col(f"  ✗ {e}", gx10.C.RED))
            elif name == "tasks":
                try:
                    ts = srv.tasks()
                    if not ts:
                        log(gx10.col("  (no tasks)", gx10.C.GRAY))
                    for t in ts:
                        log(f"  {t.get('status','?'):11} {t.get('id','?'):10} "
                            f"{t.get('type','?'):14} {t.get('title','')}")
                except Exception as e:  # noqa: BLE001
                    log(gx10.col(f"  ✗ {e}", gx10.C.RED))
            elif name == "pending":
                try:
                    ps = srv.pending()
                    if not ps:
                        log(gx10.col("  (no open handovers)", gx10.C.GRAY))
                    for it in ps:
                        log(f"  {it.get('id'):10} {it.get('agent','?'):7} "
                            f"{it.get('type','?'):14} {it.get('title','')}")
                except Exception as e:  # noqa: BLE001
                    log(gx10.col(f"  ✗ {e}", gx10.C.RED))
            elif name == "work":
                futs = client.dispatch_pending(srv, codedir, pool, claimed, log=log)
                log(gx10.col(f"  → {len(futs)} handover(s) started (parallel)", gx10.C.CYAN)
                    if futs else gx10.col("  (no new handovers)", gx10.C.GRAY))
            elif name == "auto":
                parts = payload.split()
                arg = parts[1].lower() if len(parts) > 1 else ""
                if arg == "on" and auto.get("stop") is None:
                    stop = threading.Event()
                    auto["stop"] = stop

                    def _loop(stop=stop):
                        while not stop.wait(5.0):
                            client.dispatch_pending(srv, codedir, pool, claimed, log=log)
                    threading.Thread(target=_loop, daemon=True).start()
                    log(gx10.col("  [AUTO] poller ON — pulls handovers continuously, in parallel", gx10.C.GREEN))
                elif arg == "off" and auto.get("stop") is not None:
                    auto["stop"].set(); auto["stop"] = None
                    log(gx10.col("  [AUTO] poller OFF", gx10.C.YELLOW))
                else:
                    log(gx10.col(f"  [AUTO] {'AN' if auto.get('stop') else 'AUS'}  |  /auto on / /auto off",
                                 gx10.C.GRAY))
            continue
        # kind in ("server", "turn") → an den Orchestrator streamen (Befehl ohne / bzw. Turn).
        _stream(payload)


# --------------------------------------------------------------------------- #
# Scrollable output pane (keeps ANSI colours; honours _SCROLL["rows"]).
# --------------------------------------------------------------------------- #
def _termsize():
    if gx10._UI_APP is not None:
        try:
            sz = gx10._UI_APP.output.get_size()
            return sz.rows, sz.columns
        except Exception:  # noqa: BLE001
            pass
    s = shutil.get_terminal_size((80, 24))
    return s.lines, s.columns


def _page_rows() -> int:
    rows, _ = _termsize()
    return max(1, rows - 6)


def _output():
    """Like gx10._get_output but scrollable: show a window of visual rows ending
    ``_SCROLL["rows"]`` rows above the bottom (0 = follow newest). Whole-line
    granularity; scrollback bounded to the last _MAX_SCROLL_LINES lines."""
    term_rows, term_cols = _termsize()
    rows = max(1, term_rows - 6)
    width = max(1, term_cols)
    with gx10._UI_LOCK:
        lines = list(gx10._UI_LINES)
        if gx10._UI_PARTIAL:
            lines.append(gx10._UI_PARTIAL)
    lines = lines[-_MAX_SCROLL_LINES:]
    heights = [gx10._visual_rows(ln, width) for ln in lines]
    total = sum(heights)
    max_skip = max(0, total - rows)
    skip = min(max(0, _SCROLL["rows"]), max_skip)
    _SCROLL["rows"] = skip                      # clamp stored value
    end = total - skip
    start = max(0, end - rows)
    out, pos = [], 0
    for ln, h in zip(lines, heights):
        if pos + h > start and pos < end:
            out.append(ln)
        pos += h
    return ANSI("\n".join(out))


def _expand_pastes(text: str) -> str:
    """Replace ``[Pasted #k +N lines]`` placeholders with the stored full text."""
    def _sub(m):
        idx = int(m.group(1)) - 1
        return _PASTES[idx] if 0 <= idx < len(_PASTES) else m.group(0)
    return _PASTE_RE.sub(_sub, text)


def _build_app(q: "Queue[str]", srv: Server) -> "Application":
    input_buf = Buffer(name="input_buf", multiline=False)
    kb = KeyBindings()

    def _cancel_fn():
        try:
            srv.cancel()
        except Exception as e:  # noqa: BLE001
            gx10._ui_print(gx10.col(f"  ✗ cancel failed: {e!r}", gx10.C.RED))

    @kb.add("enter")
    def _enter(event):
        raw = input_buf.text.strip()
        input_buf.reset()
        _SCROLL["rows"] = 0                       # new input -> jump back to the bottom
        if not raw:
            q.put(""); return
        expanded = _expand_pastes(raw)            # placeholders -> full text
        gx10._ui_print(gx10.col(f"\n[Du] > {raw}", gx10.C.BOLD))  # show compact
        _PASTES.clear()
        q.put(expanded)

    @kb.add(Keys.BracketedPaste)
    def _paste(event):
        data = event.data
        if "\n" in data.strip():                  # multi-line -> show compressed
            n = data.count("\n") + 1
            _PASTES.append(data)
            event.current_buffer.insert_text(f"[Pasted #{len(_PASTES)} +{n} lines]")
        else:
            event.current_buffer.insert_text(data)

    @kb.add("pageup")
    def _pgup(event):
        _SCROLL["rows"] += max(1, _page_rows() - 1)

    @kb.add("pagedown")
    def _pgdn(event):
        _SCROLL["rows"] = max(0, _SCROLL["rows"] - max(1, _page_rows() - 1))

    @kb.add("c-c")
    def _ctrl_c(event):
        if gx10._status["thinking"]:
            gx10._ui_print(gx10.col("  ⨯ cancel requested …", gx10.C.YELLOW))
            # non-blocking: cancel the running server turn
            threading.Thread(target=_cancel_fn, daemon=True).start()
        # sonst: ignorieren (exit zum Beenden)

    @kb.add("c-d")
    def _ctrl_d(event):
        q.put("\x04")

    layout = Layout(HSplit([
        Window(content=FormattedTextControl(_output, focusable=False),
               wrap_lines=True),
        Window(height=1, char="─"),
        Window(content=BufferControl(buffer=input_buf, focusable=True), height=1,
               get_line_prefix=lambda i, wrap_count: "│ [Du] > "),
        Window(height=1, char="─"),
        Window(content=FormattedTextControl(_toolbar, focusable=False), height=3),
    ]))
    return Application(layout=layout, key_bindings=kb, full_screen=True,
                       refresh_interval=gx10.UI_REFRESH_INTERVAL, mouse_support=False)


def run_tui(srv: Server, codedir: Path, max_agents: int) -> None:
    _STATUS["server"] = srv.base
    _STATUS["codedir"] = str(codedir)

    q: "Queue[str]" = Queue()
    app = _build_app(q, srv)
    gx10._UI_APP = app  # _ui_print now routes into the output pane

    gx10._ui_print(gx10.col("  Ironclad Orchestrator — full-screen client", gx10.C.GREEN))
    gx10._ui_print(gx10.col(f"  Server : {srv.base}", gx10.C.GRAY))
    gx10._ui_print(gx10.col(f"  Code   : {codedir}", gx10.C.GRAY))
    gx10._ui_print(gx10.col("  Type freely for a turn · /help for commands · exit", gx10.C.GRAY))
    gx10._ui_print(gx10.col("  PageUp/PageDown = scroll history · multi-line paste is compressed", gx10.C.GRAY))

    stop = threading.Event()
    pool = ThreadPoolExecutor(max_workers=max_agents, thread_name_prefix="codeagent")
    claimed: set = set()
    auto: Dict[str, Any] = {"stop": None}

    threading.Thread(target=_poller, args=(srv, stop), daemon=True).start()
    threading.Thread(target=_worker, args=(srv, codedir, q, app, pool, claimed, auto),
                     daemon=True).start()
    try:
        app.run()
    finally:
        stop.set()
        if auto.get("stop"):
            auto["stop"].set()
        pool.shutdown(wait=False, cancel_futures=True)
        gx10._UI_APP = None


def main() -> None:
    p = argparse.ArgumentParser(description="Ironclad full-screen TUI client")
    p.add_argument("--server", default=client.DEFAULT_SERVER)
    p.add_argument("--codedir", default=".")
    p.add_argument("--max-agents", type=int, default=client.DEFAULT_MAX_AGENTS)
    args = p.parse_args()
    if os.name == "nt":
        os.system("")
    srv = Server(args.server)
    codedir = Path(args.codedir).expanduser().resolve()
    if not HAS_PT:
        print("  [WARN] prompt_toolkit missing — pip install prompt_toolkit for the TUI.")
        print("         Falling back to the plain line REPL.\n")
        client.repl(srv, codedir, max_agents=args.max_agents)
        return
    run_tui(srv, codedir, args.max_agents)


if __name__ == "__main__":
    main()
