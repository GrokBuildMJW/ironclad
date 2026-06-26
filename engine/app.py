"""Ironclad CLI — Textual front-end (the new client).

> **A real TUI framework, like Claude Code's Ink — for the same reasons.** A component
> model + layout engine + diffing compositor is what a streaming-agent UI needs (tokens
> flowing while you scroll a long history). Textual is the Python sibling of that idea.

This is **purely a front-end**. It reuses the existing seam — the orchestrator's HTTP API
via :class:`client.Server` (streaming + the local tool-bridge), :func:`commands.classify`,
and :func:`client.dispatch_pending`. It touches nothing in the server / engine / bridge,
so the server/client separation and the core boundary are untouched. The old clients
(:mod:`tui`, :mod:`client`) keep working; this ships alongside as ``ironclad-next`` until
we switch.

Layout (full-screen, so the status line is pinned and never drifts):

    ┌ chat log (scrolls; native in-app select/copy/wheel) ┐
    │ …                                                   │
    ├─────────────────────────────────────────────────────┤
    │ ❯ input                                             │
    │ model … · ● conn · ○ watch · ○ auto · 1P/0IP/4D · …  │  ← pinned status
    └─────────────────────────────────────────────────────┘
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.error
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Input, Static
from rich.console import Group
from rich.text import Text

import client
from client import Server
from commands import HELP_TEXT, classify

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _clean(line: str) -> str:
    return _ANSI_RE.sub("", line)


def _write_clipboard(text: str) -> bool:
    """Write text to the OS clipboard directly — bypasses OSC-52 (often disabled in
    Windows Terminal), so copy works regardless of terminal escape settings."""
    if not text:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes
            CF_UNICODETEXT, GMEM_MOVEABLE = 13, 0x0002
            u, k = ctypes.windll.user32, ctypes.windll.kernel32
            k.GlobalAlloc.restype = ctypes.c_void_p
            k.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
            k.GlobalLock.restype = ctypes.c_void_p
            k.GlobalLock.argtypes = [ctypes.c_void_p]
            k.GlobalUnlock.argtypes = [ctypes.c_void_p]
            u.OpenClipboard.argtypes = [wintypes.HWND]
            u.SetClipboardData.restype = ctypes.c_void_p
            u.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
            data = text.encode("utf-16-le") + b"\x00\x00"
            h = k.GlobalAlloc(GMEM_MOVEABLE, len(data))
            ptr = k.GlobalLock(h)
            ctypes.memmove(ptr, data, len(data))
            k.GlobalUnlock(h)
            if not u.OpenClipboard(None):
                return False
            try:
                u.EmptyClipboard()
                u.SetClipboardData(CF_UNICODETEXT, h)   # clipboard takes ownership of h
            finally:
                u.CloseClipboard()
            return True
        except Exception:  # noqa: BLE001
            return False
    import subprocess
    for cmd in (["pbcopy"], ["xclip", "-selection", "clipboard"], ["xsel", "-b"], ["clip"]):
        try:
            p = subprocess.run(cmd, input=text, text=True, timeout=2)
            if p.returncode == 0:
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _is_scaffold(c: str) -> bool:
    """Orchestrator role labels that don't belong in the chat ([GX10], [Qwen (planning)])."""
    return (c == "[GX10]" or (c.startswith("[Qwen") and c.endswith("]"))
            or (c.startswith("[") and "planning" in c and c.endswith("]")))


class IroncladApp(App):
    """The streaming chat UI. One worker streams a turn into the log; another polls
    status into the footer. All HTTP/bridge logic is reused from :class:`client.Server`."""

    CSS = """
    Screen { background: $surface; }
    #logwrap { height: 1fr; background: $surface; scrollbar-size-vertical: 1; }
    #log { padding: 0 1; width: 1fr; height: auto; background: $surface; }
    #inp { height: 1; border: none; padding: 0 1; background: $panel; }
    #status { height: 1; padding: 0 1; color: $text-muted; background: $surface; }
    """

    BINDINGS = [
        Binding("escape", "cancel_turn", "cancel turn", show=True),
        Binding("ctrl+c", "copy", "copy selection", priority=True, show=True),
        Binding("ctrl+q", "quit", "quit", show=True),
    ]

    def __init__(self, srv: Server, codedir: Path, max_agents: int) -> None:
        super().__init__()
        self.srv = srv
        self.codedir = codedir
        self.max_agents = max_agents
        self._pool = ThreadPoolExecutor(max_workers=max_agents, thread_name_prefix="codeagent")
        self._claimed: set = set()
        self._thinking = False
        self._stop = False
        self._spin = 0
        self._think_t0 = 0.0
        self._last_response = ""
        self._loglines: list = []   # chat history renderables (a selectable Static)
        self._status: Dict[str, Any] = {
            "model": "?", "connected": False, "watcher": None, "autopilot": None,
            "pending": 0, "in_progress": 0, "done": 0, "perf": "", "agent": "",
        }

    # ----- layout -----------------------------------------------------------
    def compose(self) -> ComposeResult:
        # The chat history is a Static inside a scroll container — Static supports
        # mouse text-selection (RichLog does not), so you can select/copy in the chat.
        yield VerticalScroll(Static(id="log", markup=False), id="logwrap")
        yield Input(id="inp", placeholder="Frag etwas · /help · exit")
        yield Static(self._status_text(), id="status")

    def _log(self, renderable) -> None:
        """Append a renderable to the chat history (Static) and scroll to the bottom.
        Safe to call only from the UI thread (use _safe_call from workers)."""
        if isinstance(renderable, str):
            renderable = Text(renderable)
        self._loglines.append(renderable)
        if len(self._loglines) > 800:           # bound re-render cost over long sessions
            del self._loglines[:200]
        try:
            self.query_one("#log", Static).update(Group(*self._loglines))
            self.query_one("#logwrap", VerticalScroll).scroll_end(animate=False)
        except Exception:  # noqa: BLE001 — widgets gone during teardown
            pass

    def on_mount(self) -> None:
        self.title = "Ironclad"
        self._log(Text("◆ Ironclad", style="bold cyan") + Text("  ·  Orchestrator Client", style="grey50"))
        self._log(Text(f"  server  {self.srv.base}", style="grey50"))
        self._log(Text(f"  code    {self.codedir}", style="grey50"))
        self._log(Text("  /help · exit · Esc = abbrechen · Strg+C = letzte Antwort kopieren", style="grey50"))
        self.query_one("#inp", Input).focus()
        self.set_interval(0.12, self._tick)   # animate the thinking indicator
        threading.Thread(target=self._poll_loop, daemon=True).start()

    # ----- input ------------------------------------------------------------
    @on(Input.Submitted, "#inp")
    def _on_submit(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        kind, name, payload = classify(text)
        if kind == "local" and name in ("exit", "quit"):
            self.exit()
            return
        if kind == "local":
            self._handle_local(name, payload)
            return
        self._log(Text(f"\n❯ {payload}", style="bold cyan"))
        self._run_turn(payload)

    def _handle_local(self, name: str, payload: str) -> None:
        try:
            if name == "help":
                self._log(HELP_TEXT)
            elif name == "health":
                self._log(Text("  " + json.dumps(self.srv.health(), ensure_ascii=False), style="grey50"))
            elif name == "tasks":
                ts = self.srv.tasks()
                if not ts:
                    self._log(Text("  (no tasks)", style="grey50"))
                for t in ts:
                    self._log(f"  {t.get('status','?'):11} {t.get('id','?'):10} "
                              f"{t.get('type','?'):14} {t.get('title','')}")
            elif name == "pending":
                ps = self.srv.pending()
                if not ps:
                    self._log(Text("  (no open handovers)", style="grey50"))
                for it in ps:
                    self._log(f"  {it.get('id'):10} {it.get('agent','?'):7} "
                              f"{it.get('type','?'):14} {it.get('title','')}")
            elif name == "coders":                         # #452 view + #454 `use <id>|auto` pin
                parts = payload.split()
                if len(parts) >= 2 and parts[1].lower() == "use":
                    res = self.srv.set_coder_pin(parts[2] if len(parts) >= 3 else "auto")
                    pin = res.get("pinned")
                    self._log(Text(f"  → pinned coder: {pin}" if pin
                                   else "  → coder pin cleared (auto: the staged agent per task)", style="cyan"))
                data = self.srv.coders()
                coding = data.get("coding_agents") or []
                pinned = data.get("pinned")
                self._log(Text(f"  pinned: {pinned}  (/coders use auto to clear)" if pinned
                               else "  routing: auto (orchestrator's staged agent per task)", style="grey50"))
                if not coding:
                    self._log(Text("  (no coding agents configured)", style="grey50"))
                for a in coding:
                    enabled = a.get("enabled", True)        # #460: False ⇒ onboarded but not yet activated
                    mark = "◌" if not enabled else ("●" if a.get("bound") else "○")
                    is_pin = pinned and str(a.get("id", "")).upper() == str(pinned).upper()
                    if not enabled:
                        suffix = "  (onboarded · disabled)"
                    elif is_pin:
                        suffix = "  ← pinned"
                    else:
                        suffix = "" if a.get("bound") else "  (binary not found)"
                    self._log(Text(f"  {mark} {str(a.get('id','?')):8} {a.get('model','—')}" + suffix, style="white"))
            elif name == "work":
                futs = client.dispatch_pending(self.srv, self.codedir, self._pool, self._claimed,
                                               log=lambda s: self._safe_call(self._log, s))
                self._log(Text(f"  → {len(futs)} handover(s) started" if futs else "  (no new handovers)",
                               style="cyan" if futs else "grey50"))
            else:
                self._log(Text(f"  (local command '{name}' not handled here)", style="grey50"))
        except urllib.error.HTTPError as e:                # #454: show the server's JSON error detail
            self._log(Text(f"  ✗ {client.http_error_msg(e)}", style="red"))
        except Exception as e:  # noqa: BLE001
            self._log(Text(f"  ✗ {e}", style="red"))

    # ----- streaming worker (own daemon thread → never blocks shutdown) -----
    def _run_turn(self, payload: str) -> None:
        if self._thinking:
            self._log(Text("  (turn läuft noch — Esc zum Abbrechen)", style="yellow"))
            return
        threading.Thread(target=self._turn_thread, args=(payload,), daemon=True).start()

    def _turn_thread(self, payload: str) -> None:
        self._think_t0 = time.time()
        self._thinking = True
        self._status["agent"] = ""                        # #453: clear last turn's coder (no stale "live")
        self._safe_call(self._refresh_status)
        partial = {"buf": "", "blank": True}
        answer: list = []                    # collected answer lines → self._last_response

        def emit_line(line: str) -> None:
            c = _clean(line).strip()
            idx = c.find("[perf]")
            if idx != -1:
                self._status["perf"] = c[idx:].replace("[perf]", "").strip()
                self._safe_call(self._refresh_status)
                return
            jdx = c.find("[agent]")                       # #453: which coder was routed → status, not chat
            if jdx != -1:
                self._status["agent"] = c[jdx + len("[agent]"):].strip()
                self._safe_call(self._refresh_status)
                return
            sdx = c.find("[search]")                       # S9: web-search summary → status footer, not chat
            if sdx != -1:
                self._status["search"] = c[sdx + len("[search]"):].strip()
                self._safe_call(self._refresh_status)
                return
            if "===" in c and "DONE" in c:
                return
            if _is_scaffold(c):
                return
            if not c:
                if partial["blank"]:
                    return
                partial["blank"] = True
            else:
                partial["blank"] = False
            answer.append(line)
            self._safe_call(self._log, line)

        def on_text(t: str) -> None:
            partial["buf"] += t
            while "\n" in partial["buf"]:
                ln, partial["buf"] = partial["buf"].split("\n", 1)
                emit_line(ln)

        try:
            self.srv.chat_stream(payload, on_text)
        except Exception as e:  # noqa: BLE001
            self._safe_call(self._log, Text(f"  ✗ /chat/stream failed: {e!r}", style="red"))
        finally:
            if partial["buf"]:
                emit_line(partial["buf"])
            self._last_response = "\n".join(answer).strip()
            self._thinking = False
            self._safe_call(self._refresh_status)

    def _safe_call(self, fn, *args) -> None:
        """call_from_thread that never raises if the app is tearing down."""
        try:
            self.call_from_thread(fn, *args)
        except Exception:  # noqa: BLE001
            pass

    def action_cancel_turn(self) -> None:
        if self._thinking:
            self._log(Text("  ⨯ cancel …", style="yellow"))
            threading.Thread(target=self._cancel_thread, daemon=True).start()

    def action_copy(self) -> None:
        """Ctrl+C: copy the mouse selection if there is one; otherwise copy the last
        assistant response (reliable, no mouse needed)."""
        try:
            sel = self.screen.get_selected_text()
        except Exception:  # noqa: BLE001
            sel = None
        if sel:
            text, what = sel, f"Auswahl ({len(sel)} Zeichen)"
            self.screen.clear_selection()
        elif self._last_response:
            text, what = self._last_response, "letzte Antwort"
        else:
            self._log(Text("  (noch nichts zum Kopieren)", style="grey50"))
            return
        if _write_clipboard(text):
            self._log(Text(f"  ✓ kopiert — {what}", style="green"))
        else:
            self._log(Text("  ✗ Zwischenablage nicht erreichbar", style="red"))

    def _cancel_thread(self) -> None:
        try:
            self.srv.cancel()
        except Exception:  # noqa: BLE001
            pass

    def _tick(self) -> None:
        if self._thinking:
            self._spin = (self._spin + 1) % len(_SPIN)
            self._refresh_status()

    # ----- status footer ----------------------------------------------------
    def _status_text(self) -> Text:
        s = self._status
        if self._thinking:
            frame = _SPIN[self._spin % len(_SPIN)]
            secs = int(time.time() - self._think_t0) if self._think_t0 else 0
            # #453: the spinner replaces the footer while thinking, so surface the live coder here too
            coder = f" · coder {s['agent']}" if s.get("agent") else ""
            return Text(f" {frame} {s['model']} denkt… {secs}s{coder}     Esc = cancel", style="cyan")
        t = Text(" model ", style="grey50")
        t.append(s["model"], style="cyan")
        t.append("  ·  ", style="grey50")
        t.append("●" if s["connected"] else "○", style="green" if s["connected"] else "red")
        t.append(" conn", style="grey50")
        t.append("  ·  ", style="grey50")
        t.append("●" if s["watcher"] else "○", style="green" if s["watcher"] else "grey50")
        t.append(" watch  ", style="grey50")
        t.append("●" if s["autopilot"] else "○", style="green" if s["autopilot"] else "grey50")
        t.append(" auto", style="grey50")
        t.append("  ·  ", style="grey50")
        t.append(f"{s['pending']}P/{s['in_progress']}IP/{s['done']}D", style="grey50")
        if s.get("agent"):                                # #453: which coder was last routed
            t.append("  ·  ", style="grey50")
            t.append(f"coder {s['agent']}", style="#7fb3d5")
        if s["perf"]:
            t.append("  ·  ", style="grey50")
            t.append(s["perf"], style="grey50")
        return t

    def _refresh_status(self) -> None:
        try:
            self.query_one("#status", Static).update(self._status_text())
        except Exception:  # noqa: BLE001 — widget gone during teardown
            pass

    def _poll_loop(self) -> None:
        while not self._stop:
            try:
                h = self.srv.health()
                self._status.update(connected=True, model=h.get("model", "?"),
                                    watcher=h.get("watcher"), autopilot=h.get("autopilot"))
                c = Counter(t.get("status") for t in self.srv.tasks())
                self._status.update(pending=c.get("pending", 0),
                                    in_progress=c.get("in_progress", 0), done=c.get("done", 0))
            except Exception:  # noqa: BLE001
                self._status["connected"] = False
            self._safe_call(self._refresh_status)
            time.sleep(2.0)

    def on_unmount(self) -> None:
        self._stop = True
        self._pool.shutdown(wait=False, cancel_futures=True)


def main() -> None:
    p = argparse.ArgumentParser(description="Ironclad CLI (Textual front-end)")
    p.add_argument("--server", default=client.DEFAULT_SERVER)
    p.add_argument("--codedir", default=".")
    p.add_argument("--max-agents", type=int, default=client.DEFAULT_MAX_AGENTS)
    args = p.parse_args()
    srv = Server(args.server, token=client.SERVER_TOKEN)
    codedir = Path(args.codedir).expanduser().resolve()
    os.chdir(codedir)   # passed-through code-tools (run_tool) act on YOUR local code root
    transport = client.Tunnel(client.TUNNEL_CMD, args.server) if client.TUNNEL_CMD else client._NullCtx()
    with transport:
        stop_hb, _ = client._establish_session(srv)
        try:
            IroncladApp(srv, codedir, args.max_agents).run()
        finally:
            if stop_hb is not None:
                stop_hb.set()
            srv.session_close()


if __name__ == "__main__":
    main()
