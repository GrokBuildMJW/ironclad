"""Thin orchestrator client — the PC side of the server/client split.

> **Connects exactly like the CLI connects to the model: plain LAN HTTP, this side
> initiates.** The orchestrator (reasoning + state) lives on the server
> (:mod:`core.engine.server`, on the Spark). This client holds nothing but the
> conversation REPL and the *code locality*: project code stays on this machine, and
> the code-agents (``claude --print``) run HERE, against the local working copy —
> never on the server.

The loop, end to end:
  1. You type → ``POST /chat`` → the server runs one orchestrator turn → its output
     prints here. ``stage_handover`` on the server creates handover files server-side.
  2. ``/pending`` (or the ``/auto`` poller) pulls handovers the server has staged.
  3. For each, the client writes the handover into the LOCAL ``summaries/handovers/``
     and runs ``claude --print`` with the local code root as cwd — so the code-agent
     edits *local* code, reading ``.claude/CLAUDE.md`` like a normal CLI session.
  4. claude writes ``summaries/feedback/{id}_{AGENT}-feedback.md`` locally; the client
     uploads it via ``POST /feedback``; the server's reconciler advances the task.

Because the client pulls (never the server pushing in), session-gating and
code-locality are structural, not enforced by extra machinery.

Secret-free, zero deps: stdlib ``urllib`` + ``subprocess`` only. Connection target
comes from ``--server`` / ``GX10_SERVER_URL`` (a private value lives in ``conf/``,
never here).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from concurrent.futures import Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Dict, List, Optional

from commands import HELP_TEXT, classify

# Default is localhost (secret-free); the real server address (e.g. the box on the
# LAN) comes from GX10_SERVER_URL / --server — private via conf/, never hard-coded here.
DEFAULT_SERVER = os.environ.get("GX10_SERVER_URL", "http://localhost:8100")
CLAUDE_BIN = os.environ.get("GX10_CLAUDE_BIN", "claude")
DEFAULT_EFFORT = os.environ.get("GX10_CLAUDE_EFFORT", "high")
#: Max local code-agents (claude --print) running at once. Each is heavy
#: (Opus/Sonnet) → conservative default; override with GX10_MAX_AGENTS.
DEFAULT_MAX_AGENTS = int(os.environ.get("GX10_MAX_AGENTS", "3"))
_MODEL_BY_AGENT = {"OPUS": "claude-opus-4-8", "SONNET": "claude-sonnet-4-6"}


# --------------------------------------------------------------------------- #
# HTTP (stdlib).
# --------------------------------------------------------------------------- #
class Server:
    def __init__(self, base_url: str, timeout: float = 600.0) -> None:
        self.base = base_url.rstrip("/")
        self.timeout = timeout

    def _req(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}

    def health(self) -> Dict[str, Any]:
        return self._req("GET", "/health")

    def chat(self, message: str) -> Dict[str, Any]:
        return self._req("POST", "/chat", {"message": message})

    def chat_stream(self, message: str, on_text) -> None:
        """Stream a turn from /chat/stream, calling ``on_text(chunk)`` as text arrives.
        Decodes UTF-8 incrementally so a multi-byte char split across socket reads is
        never mangled. Blocks until the server closes the connection (turn done)."""
        import codecs
        body = json.dumps({"message": message}).encode("utf-8")
        req = urllib.request.Request(self.base + "/chat/stream", data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        dec = codecs.getincrementaldecoder("utf-8")("replace")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            while True:
                chunk = resp.read(256)
                if not chunk:
                    break
                text = dec.decode(chunk)
                if text:
                    on_text(text)
        tail = dec.decode(b"", final=True)
        if tail:
            on_text(tail)

    def cancel(self) -> Dict[str, Any]:
        """Abort the turn currently running on the server (sets its cancel event)."""
        return self._req("POST", "/cancel", {})

    def tasks(self) -> List[Dict[str, Any]]:
        return self._req("GET", "/tasks").get("tasks", [])

    def pending(self) -> List[Dict[str, Any]]:
        return self._req("GET", "/pending").get("pending", [])

    def feedback(self, task_id: str, agent: str, content: str) -> Dict[str, Any]:
        return self._req("POST", "/feedback",
                         {"task_id": task_id, "agent": agent, "content": content})


# --------------------------------------------------------------------------- #
# Local code-agent execution (the code-locality half).
# --------------------------------------------------------------------------- #
def _run_handover(item: Dict[str, Any], codedir: Path, log=print) -> Optional[str]:
    """Run a single staged handover LOCALLY with ``claude --print`` and return the
    feedback text it wrote (or None if it produced none).

    The handover content is materialised into the local ``summaries/handovers/`` so
    claude reads it exactly as in a normal session; claude is expected to write
    ``summaries/feedback/{id}_{AGENT}-feedback.md`` locally, which we read back.
    ``log`` is the output sink (default ``print``; the TUI passes ``gx10._ui_print``
    so messages land in the full-screen pane, not over the layout)."""
    tid = item.get("id") or ""
    agent = (item.get("agent") or "OPUS").upper()
    ho_name = item.get("handover_file") or f"{tid}_{agent}.md"
    ho_text = item.get("handover") or ""

    ho_dir = codedir / "summaries" / "handovers"
    ho_dir.mkdir(parents=True, exist_ok=True)
    (ho_dir / ho_name).write_text(ho_text, encoding="utf-8")

    model = item.get("model") or _MODEL_BY_AGENT.get(agent, "claude-opus-4-8")
    if str(model).startswith("kimi"):
        model = _MODEL_BY_AGENT.get(agent, "claude-opus-4-8")
    effort = item.get("effort") or DEFAULT_EFFORT
    prompt = (f"Autonomously read and work the handover {ho_name} in "
              f"summaries/handovers/. Follow the instructions in .claude/CLAUDE.md.")

    argv = [CLAUDE_BIN, "--model", str(model), "--effort", str(effort), "--print", prompt]
    log(f"  → claude (local): {tid} ({agent}, {model}, effort={effort})  cwd={codedir}")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    try:
        proc = subprocess.run(argv, cwd=str(codedir), env=env,
                              stdin=subprocess.DEVNULL, text=True)
    except FileNotFoundError:
        log(f"  ✗ claude binary '{CLAUDE_BIN}' not found "
            f"(set GX10_CLAUDE_BIN) — handover {tid} skipped")
        return None
    rc = proc.returncode

    fb_path = codedir / "summaries" / "feedback" / f"{tid}_{agent}-feedback.md"
    if fb_path.exists():
        return fb_path.read_text(encoding="utf-8")
    log(f"  ⚠ claude exited (exit {rc}) without a feedback file {fb_path.name}")
    return None


def _process_one(srv: Server, codedir: Path, item: Dict[str, Any], claimed: set,
                 log=print) -> bool:
    """One pool job: run the handover locally, upload its feedback. On any failure
    the task is UNclaimed so the next poll retries it. Returns True on a clean upload."""
    tid = item.get("id") or ""
    agent = (item.get("agent") or "OPUS").upper()
    try:
        fb = _run_handover(item, codedir, log=log)
        if fb:
            res = srv.feedback(tid, agent, fb)
            log(f"  ✓ feedback uploaded: {tid} → {res.get('feedback_file')}")
            return True
        log(f"  ⚠ {tid}: no feedback produced — will retry on the next poll")
    except urllib.error.URLError as e:
        log(f"  ✗ {tid}: upload/network failed: {e}")
    except Exception as e:  # noqa: BLE001
        log(f"  ✗ {tid}: code-agent failed: {e!r}")
    claimed.discard(tid)
    return False


def dispatch_pending(srv: Server, codedir: Path, pool: ThreadPoolExecutor,
                     claimed: set, log=print) -> List[Future]:
    """Pull pending handovers and submit every unclaimed one to the bounded pool —
    NON-blocking. Concurrency is the pool's ``max_workers`` (= max parallel
    ``claude --print``). Returns the futures submitted this call."""
    try:
        pending = srv.pending()
    except urllib.error.URLError as e:
        log(f"  ✗ /pending unreachable: {e}")
        return []
    futures: List[Future] = []
    for item in pending:
        tid = item.get("id") or ""
        if not tid or tid in claimed:
            continue
        claimed.add(tid)  # sofort beanspruchen → kein Doppel-Launch beim nächsten Poll
        futures.append(pool.submit(_process_one, srv, codedir, item, claimed, log))
    return futures


# --------------------------------------------------------------------------- #
# REPL.
# --------------------------------------------------------------------------- #
def _print_tasks(tasks: List[Dict[str, Any]]) -> None:
    if not tasks:
        print("  (no tasks)")
        return
    for t in tasks:
        print(f"  {t.get('status','?'):11} {t.get('id','?'):10} "
              f"{t.get('type','?'):14} {t.get('title','')}")


def repl(srv: Server, codedir: Path, max_agents: int = DEFAULT_MAX_AGENTS) -> None:
    try:
        h = srv.health()
        print(f"  connected: {srv.base}  |  model {h.get('model')}  |  "
              f"vLLM {h.get('base_url')}")
    except urllib.error.URLError as e:
        print(f"  ⚠ server {srv.base} unreachable: {e}")
    print(f"  code root (local): {codedir}  |  max parallel code-agents: {max_agents}")
    print(HELP_TEXT)

    claimed: set = set()
    pool = ThreadPoolExecutor(max_workers=max_agents, thread_name_prefix="codeagent")
    auto_stop: Optional[threading.Event] = None

    def _auto_loop(stop: threading.Event) -> None:
        while not stop.wait(5.0):
            try:
                dispatch_pending(srv, codedir, pool, claimed)
            except Exception as e:  # noqa: BLE001
                print(f"  ✗ auto-poll: {e!r}")

    while True:
        try:
            line = input("\n[Du] > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        kind, name, payload = classify(line)
        if kind == "empty":
            continue
        if kind == "local" and name in ("exit", "quit"):
            break
        if kind == "local":
            if name == "help":
                print(HELP_TEXT)
            elif name == "health":
                try:
                    print("  " + json.dumps(srv.health(), ensure_ascii=False))
                except urllib.error.URLError as e:
                    print(f"  ✗ {e}")
            elif name == "tasks":
                try:
                    _print_tasks(srv.tasks())
                except urllib.error.URLError as e:
                    print(f"  ✗ {e}")
            elif name == "pending":
                try:
                    p = srv.pending()
                    if not p:
                        print("  (no open handovers)")
                    for it in p:
                        print(f"  {it.get('id'):10} {it.get('agent','?'):7} "
                              f"{it.get('type','?'):14} {it.get('title','')}")
                except urllib.error.URLError as e:
                    print(f"  ✗ {e}")
            elif name == "work":
                futures = dispatch_pending(srv, codedir, pool, claimed)
                if not futures:
                    print("  (no new handovers)")
                else:
                    print(f"  → {len(futures)} handover(s) started (≤{max_agents} parallel), waiting ...")
                    done_set, _ = wait(futures)
                    ok = sum(1 for f in done_set if f.result() is True)
                    print(f"  done: {ok}/{len(futures)} cleanly uploaded")
            elif name == "auto":
                parts = payload.split()
                arg = parts[1].lower() if len(parts) > 1 else ""
                if arg == "on":
                    if auto_stop is None:
                        auto_stop = threading.Event()
                        threading.Thread(target=_auto_loop, args=(auto_stop,), daemon=True).start()
                        print(f"  [AUTO] poller ON — pulls handovers every 5s, ≤{max_agents} parallel")
                    else:
                        print("  [AUTO] already running")
                elif arg == "off":
                    if auto_stop is not None:
                        auto_stop.set()
                        auto_stop = None
                        print("  [AUTO] poller OFF")
                    else:
                        print("  [AUTO] was not active")
                else:
                    print(f"  [AUTO] {'AN' if auto_stop else 'AUS'}  |  /auto on / /auto off")
            continue
        # kind in ("server", "turn") → an den Orchestrator (Server-Befehl ohne / bzw. Turn).
        try:
            res = srv.chat(payload)
            out = res.get("output", "")
            if out:
                print(out, end="" if out.endswith("\n") else "\n")
        except urllib.error.URLError as e:
            print(f"  ✗ /chat failed: {e}")

    if auto_stop is not None:
        auto_stop.set()
    pool.shutdown(wait=False, cancel_futures=True)


def main() -> None:
    p = argparse.ArgumentParser(description="Ironclad thin orchestrator client")
    p.add_argument("--server", default=DEFAULT_SERVER,
                   help=f"Orchestrator server URL (default {DEFAULT_SERVER})")
    p.add_argument("--codedir", default=".",
                   help="Local code root (cwd for claude --print; contains .claude/CLAUDE.md)")
    p.add_argument("--max-agents", type=int, default=DEFAULT_MAX_AGENTS,
                   help=f"Max parallel local code-agents (default {DEFAULT_MAX_AGENTS})")
    args = p.parse_args()
    if os.name == "nt":
        os.system("")
    srv = Server(args.server)
    codedir = Path(args.codedir).expanduser().resolve()
    repl(srv, codedir, max_agents=args.max_agents)


if __name__ == "__main__":
    main()
