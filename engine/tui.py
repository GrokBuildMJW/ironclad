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
import os
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
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import HSplit, Window
    from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
    HAS_PT = True
except ImportError:
    HAS_PT = False

import gx10  # UI primitives (openai-frei importierbar)
import client
from client import Server

import re as _re

_PERF_RE = _re.compile(r"\[perf\][^\n]*")

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
        mid = f"  {frame}  {st['label']}...   Strg+C = abbrechen   "
    else:
        mid = "  Ironclad Thin-Client  ·  streaming  |  exit = Beenden   "
    line3 = (f"     {_STATUS['model']}  ·  {_STATUS['perf'] or '—'}  ·  "
             f"tasks {_STATUS['pending']}P/{_STATUS['in_progress']}IP/{_STATUS['done']}D"
             f"  ·  {_STATUS['server']}")
    return [
        ("fg:ansiblue bold", " ██ "),
        ("bold", "GX10 CLI"),
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
        except Exception:  # noqa: BLE001 — Netz weg → rot, weiter pollen
            _STATUS["connected"] = False
        if gx10._UI_APP is not None:
            gx10._UI_APP.invalidate()


# --------------------------------------------------------------------------- #
# Worker: consume input, stream turns from the server, run local code-agents.
# --------------------------------------------------------------------------- #
def _worker(srv: Server, codedir: Path, q: "Queue[str]", app: "Application",
            pool: ThreadPoolExecutor, claimed: set, auto: Dict[str, Any]) -> None:
    log = gx10._ui_print

    def _on_text(t: str) -> None:
        gx10._ui_print(t, end="")  # Server-Chunks tragen ihre eigenen \n
        m = _PERF_RE.search(t)
        if m:
            _STATUS["perf"] = m.group(0).strip()

    while True:
        text = q.get().strip()
        if text == "\x04":
            app.exit(); return
        if not text:
            continue
        low = text.lower()
        if low == "exit":
            app.exit(); return
        if low == "/work":
            futs = client.dispatch_pending(srv, codedir, pool, claimed, log=log)
            log(gx10.col(f"  → {len(futs)} Handover gestartet (parallel)", gx10.C.CYAN)
                if futs else gx10.col("  (keine neuen Handover)", gx10.C.GRAY))
            continue
        if low.startswith("/auto"):
            arg = low.split()[-1] if len(low.split()) > 1 else ""
            if arg == "on" and auto.get("stop") is None:
                stop = threading.Event()
                auto["stop"] = stop

                def _loop(stop=stop):
                    while not stop.wait(5.0):
                        client.dispatch_pending(srv, codedir, pool, claimed, log=log)
                threading.Thread(target=_loop, daemon=True).start()
                log(gx10.col("  [AUTO] Poller AN — zieht Handover laufend, parallel", gx10.C.GREEN))
            elif arg == "off" and auto.get("stop") is not None:
                auto["stop"].set(); auto["stop"] = None
                log(gx10.col("  [AUTO] Poller AUS", gx10.C.YELLOW))
            else:
                log(gx10.col(f"  [AUTO] {'AN' if auto.get('stop') else 'AUS'}  |  /auto on / /auto off",
                             gx10.C.GRAY))
            continue
        # sonst: Turn vom Server streamen.
        gx10._status["thinking"] = True
        gx10._status["label"] = "Qwen"
        try:
            srv.chat_stream(text, _on_text)
        except Exception as e:  # noqa: BLE001
            gx10._ui_print(gx10.col(f"  ✗ /chat/stream fehlgeschlagen: {e!r}", gx10.C.RED))
        finally:
            gx10._status["thinking"] = False
            if gx10._UI_APP is not None:
                gx10._UI_APP.invalidate()


def _build_app(q: "Queue[str]") -> "Application":
    input_buf = Buffer(name="input_buf", multiline=False)
    kb = KeyBindings()

    @kb.add("enter")
    def _enter(event):
        text = input_buf.text.strip()
        input_buf.reset()
        if text:
            gx10._ui_print(gx10.col(f"\n[Du] > {text}", gx10.C.BOLD))
        q.put(text)

    @kb.add("c-c")
    def _ctrl_c(event):
        if gx10._status["thinking"]:
            gx10._ui_print(gx10.col("  (Remote-Turn läuft — Abbrechen noch nicht verdrahtet)",
                                    gx10.C.GRAY))
        # sonst: ignorieren (exit zum Beenden)

    @kb.add("c-d")
    def _ctrl_d(event):
        q.put("\x04")

    layout = Layout(HSplit([
        Window(content=FormattedTextControl(gx10._get_output, focusable=False),
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
    app = _build_app(q)
    gx10._UI_APP = app  # _ui_print routet ab jetzt in den Output-Pane

    gx10._ui_print(gx10.col("  Ironclad Orchestrator — Vollbild-Client", gx10.C.GREEN))
    gx10._ui_print(gx10.col(f"  Server : {srv.base}", gx10.C.GRAY))
    gx10._ui_print(gx10.col(f"  Code   : {codedir}", gx10.C.GRAY))
    gx10._ui_print(gx10.col("  Tippe frei für einen Turn · /work · /auto on|off · exit", gx10.C.GRAY))

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
        print("  [WARN] prompt_toolkit fehlt — pip install prompt_toolkit für das TUI.")
        print("         Falle auf die einfache Zeilen-REPL zurück.\n")
        client.repl(srv, codedir, max_agents=args.max_agents)
        return
    run_tui(srv, codedir, args.max_agents)


if __name__ == "__main__":
    main()
