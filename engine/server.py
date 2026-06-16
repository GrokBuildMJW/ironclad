"""Headless orchestrator server — the Spark side of the server/client split.

> **The split, in one sentence.** The orchestrator engine (:mod:`gx10`) is reached
> like the model: plain LAN HTTP, client-initiated. This module drives the engine
> **headless** (no prompt_toolkit UI) and exposes a tiny HTTP API. The thin client
> (:mod:`core.engine.client`) connects exactly like the CLI connects to vLLM.

Design (confirmed): the server holds the *reasoning + state* — the GX10 turn loop,
the TaskStore, ``stage_handover`` / ``advance_pipeline``, and the feedback-side
reconciler. It NEVER reaches into the client. The client owns *code locality*: it
pulls handovers, runs the code-agents (``claude --print``) against its LOCAL code,
and posts feedback back. Because the client initiates every exchange (pull), session
gating and code-locality fall out for free.

How headless output works: when no UI is mounted, :func:`gx10._ui_print` normally
falls back to ``print``. We instead install a thread-local capture hook
(``gx10._UI_SINK``) so a ``POST /chat`` request collects exactly the output its own
turn produced, while background threads (reconciler / queue consumer) log to the
server's stdout. The engine's agent state is serialized behind a single lock — one
turn at a time — so a request and a reconciler-driven advance never race.

Endpoints (all JSON; trust model = home LAN, no auth — same as the vLLM port):
  GET  /health   → liveness + effective config summary
  POST /chat     → ``{"message": str}`` → run one orchestrator turn, return captured output
  GET  /tasks    → TaskStore snapshot (all statuses)
  GET  /pending  → tasks awaiting a local code-agent (pending + handover present)
  POST /feedback → ``{"task_id","agent","content"}`` → drop the feedback file the
                   reconciler advances on
  POST /fanout   → ``{"prompts":[...], "system"?, "max_tokens"?, "temperature"?,
                   "think"?}`` → run independent reasoning prompts CONCURRENTLY against
                   the local model (co-located with the GPU); results in input order.
                   Stateless — does not take the agent lock.

Secret-free: imports only :mod:`gx10` + stdlib. All connection details come from the
config tree (``conf/…``), never hard-coded here.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

# Die Engine wird als Standalone-Script-Verzeichnis geführt (gx10.py legt core/ auf
# sys.path, nicht als Paket). Wir spiegeln das: dieses Verzeichnis (core/engine) auf
# den Pfad, dann gx10 absolut importieren — funktioniert als Script UND als Modul.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import gx10  # noqa: E402
from workers import ReasoningWorkers  # noqa: E402

# --------------------------------------------------------------------------- #
# Headless output capture — one buffer per request thread.
# --------------------------------------------------------------------------- #
_CAPTURE = threading.local()

#: Serializes all access to the shared GX10 agent (its ``messages`` list is not
#: thread-safe). A /chat turn and a reconciler-driven advance take turns.
_AGENT_LOCK = threading.Lock()


def _capture_sink(text: str) -> None:
    """``gx10._UI_SINK`` hook. Output produced inside a request that registered an
    ``emit`` callable on this thread goes there (buffered for /chat, streamed for
    /chat/stream); everything else (background threads) goes to the server log."""
    emit = getattr(_CAPTURE, "emit", None)
    if emit is not None:
        emit(text)
    else:
        # Background reconciler / queue-consumer output → server stdout (the log).
        try:
            os.write(1, text.encode("utf-8", "replace"))
        except OSError:
            pass


class _Captured:
    """Context manager: collect this thread's ``_ui_print`` output into a string."""

    def __enter__(self) -> "_Captured":
        self._buf: list[str] = []
        _CAPTURE.emit = self._buf.append
        return self

    def __exit__(self, *exc: Any) -> None:
        self._text = "".join(self._buf)
        _CAPTURE.emit = None

    @property
    def text(self) -> str:
        return getattr(self, "_text", "")


class _Streamed:
    """Context manager: route this thread's ``_ui_print`` output to *write* live (a
    callable taking a str chunk), instead of buffering it."""

    def __init__(self, write: Any) -> None:
        self._write = write

    def __enter__(self) -> "_Streamed":
        _CAPTURE.emit = self._write
        return self

    def __exit__(self, *exc: Any) -> None:
        _CAPTURE.emit = None


# --------------------------------------------------------------------------- #
# Bootstrap — same config pipeline + agent construction the CLI uses (main()),
# but headless: no prompt_toolkit, autopilot forced OFF (the client launches
# code-agents, never the server), watcher ON (the reconciler must advance).
# --------------------------------------------------------------------------- #
def bootstrap(config_path: Optional[str] = None) -> Tuple[gx10.GX10, Dict[str, Any], Optional[Path], Path]:
    cfg = gx10._code_defaults()
    cfg_path = gx10._resolve_config_source(config_path)
    cfg = gx10._deep_merge(cfg, gx10._load_config_tree(cfg_path))
    cfg = gx10._apply_env(cfg)
    gx10._apply_config(cfg)
    gx10._EFFECTIVE_CFG = cfg
    gx10._CFG_SOURCE = cfg_path

    # Prompt vor dem chdir absolut auflösen (relativ → SCRIPT_DIR), wie in main().
    prompt_cfg = cfg["paths"]["system_prompt"]
    prompt_abs = ""
    if prompt_cfg:
        pp = Path(prompt_cfg).expanduser()
        prompt_abs = str(pp if pp.is_absolute() else (gx10.SCRIPT_DIR / pp))

    workdir = Path(cfg["paths"]["workdir"]).expanduser().resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    os.chdir(workdir)

    api_key = os.environ.get(cfg["connection"]["api_key_env"]) or gx10.DEFAULT_API_KEY
    agent = gx10.GX10(
        base_url=cfg["connection"]["base_url"],
        api_key=api_key,
        model=cfg["connection"]["model"],
        prompt_path=prompt_abs,
        stream=bool(cfg["generation"]["stream"]),
        max_tokens=int(cfg["generation"]["max_tokens"]),
        thinking_mode=cfg["generation"]["thinking_mode"],
        platform=gx10.PLATFORM,
        onboarding=gx10.ONBOARDING_MODE,
    )
    if Path(gx10.SESSION_FILE).exists():
        try:
            agent.load_session()
        except Exception:
            pass

    # Server-Politik: NIE selbst claude starten (das ist Client-Sache); aber den
    # Feedback-Reconciler aktiv halten, damit gepostetes Feedback Tasks vorrückt.
    gx10.AUTOPILOT_ENABLED = False
    gx10._WATCHER_ENABLED = True
    return agent, cfg, cfg_path, workdir


# --------------------------------------------------------------------------- #
# Background: feedback-side reconciler + a headless queue consumer.
# The reconciler enqueues structured ADVANCE commands onto gx10._INPUT_QUEUE;
# the consumer applies them (and any plain prompts) under the agent lock.
# --------------------------------------------------------------------------- #
def _queue_consumer(agent: gx10.GX10, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            item = gx10._INPUT_QUEUE.get(timeout=1.0)
        except Exception:
            continue
        item = (item or "").strip()
        if not item:
            continue
        if item.startswith(gx10._LAUNCH_CMD):
            # Launch ist Client-Sache — der Server startet keine code-agents.
            continue
        if item.startswith(gx10._ADVANCE_CMD):
            parts = item.split("\x00")  # ['', 'advance', tid, agent]
            if len(parts) >= 4:
                tid, agent_adv = parts[2], parts[3]
                with _AGENT_LOCK:
                    try:
                        res = gx10._advance_pipeline(tid, agent_adv)
                    except Exception as e:  # noqa: BLE001
                        res = f"ERROR: {e!r}"
                print(f"[ADVANCE] {tid} ({agent_adv}): {res.splitlines()[0] if res else res}",
                      flush=True)
            continue
        # Plain prompt (e.g. autoplan) → normaler Turn.
        with _AGENT_LOCK:
            try:
                gx10._dispatch(agent, item)
            except Exception as e:  # noqa: BLE001
                print(f"[QUEUE] dispatch failed: {e!r}", flush=True)


# --------------------------------------------------------------------------- #
# HTTP handlers.
# --------------------------------------------------------------------------- #
def _pending_handovers() -> list[Dict[str, Any]]:
    """Tasks awaiting a local code-agent: status pending AND a handover file present.
    The client pulls these, runs ``claude --print`` locally, posts feedback back."""
    store = gx10._store()
    out: list[Dict[str, Any]] = []
    for task in store.list("pending"):
        tid = task.get("id") or ""
        ho = gx10._find_handover(tid)
        if not ho:
            continue
        model, effort = gx10._parse_handover_meta(ho)
        try:
            content = ho.read_text(encoding="utf-8")
        except OSError:
            content = ""
        out.append({
            "id": tid,
            "agent": gx10._task_agent(task) or gx10._agent_from_handover(ho.name),
            "title": task.get("title"),
            "type": task.get("type"),
            "handover_file": ho.name,
            "handover": content,
            "model": model,
            "effort": effort,
        })
    return out


def _write_feedback(task_id: str, agent: str, content: str) -> str:
    """Drop ``{task_id}_{AGENT}-feedback.md`` into the watch dir. The server-side
    reconciler detects it (mtime-stable) and advances the task."""
    d = Path(gx10.WATCHER_FEEDBACK_DIR)
    d.mkdir(parents=True, exist_ok=True)
    agent_u = (agent or "OPUS").upper()
    fb = d / f"{task_id}_{agent_u}-feedback.md"
    fb.write_text(content, encoding="utf-8")
    return str(fb)


class _Handler(BaseHTTPRequestHandler):
    server_version = "Ironclad-Orchestrator/0"

    # Der GX10-Agent + Config + Reasoning-Worker werden vom Server injiziert.
    agent: gx10.GX10
    cfg: Dict[str, Any]
    workers: ReasoningWorkers

    def log_message(self, fmt: str, *args: Any) -> None:  # leiser, eine Zeile
        print(f"[http] {self.address_string()} {fmt % args}", flush=True)

    # ── helpers ──────────────────────────────────────────────
    def _send(self, code: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> Dict[str, Any]:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except (ValueError, UnicodeDecodeError):
            return {}

    # ── routes ───────────────────────────────────────────────
    def do_GET(self) -> None:
        try:
            if self.path == "/health":
                self._send(200, {
                    "ok": True,
                    "model": self.agent.model,
                    "base_url": self.cfg["connection"]["base_url"],
                    "workdir": os.getcwd(),
                    "watcher": gx10._WATCHER_ENABLED,
                    "autopilot": gx10.AUTOPILOT_ENABLED,
                    "language": gx10.LANGUAGE,
                })
            elif self.path == "/tasks":
                self._send(200, {"tasks": gx10._store().list()})
            elif self.path == "/pending":
                self._send(200, {"pending": _pending_handovers()})
            else:
                self._send(404, {"ok": False, "error": f"no route {self.path}"})
        except Exception as e:  # noqa: BLE001
            self._send(500, {"ok": False, "error": repr(e)})

    def do_POST(self) -> None:
        try:
            if self.path == "/chat":
                data = self._read_json()
                message = (data.get("message") or "").strip()
                if not message:
                    self._send(400, {"ok": False, "error": "missing 'message'"})
                    return
                with _Captured() as cap:
                    with _AGENT_LOCK:
                        gx10._dispatch(self.agent, message)
                self._send(200, {"ok": True, "output": cap.text})
            elif self.path == "/chat/stream":
                data = self._read_json()
                message = (data.get("message") or "").strip()
                if not message:
                    self._send(400, {"ok": False, "error": "missing 'message'"})
                    return
                # Live: kein Content-Length, Connection: close → der Client liest bis
                # EOF. Jeder _ui_print-Chunk wird sofort auf den Socket geflusht.
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()

                def _write(text: str) -> None:
                    try:
                        self.wfile.write(text.encode("utf-8", "replace"))
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        pass  # Client weg → Turn läuft serverseitig zu Ende
                with _Streamed(_write):
                    with _AGENT_LOCK:
                        gx10._dispatch(self.agent, message)
            elif self.path == "/feedback":
                data = self._read_json()
                tid = (data.get("task_id") or "").strip()
                content = data.get("content") or ""
                agent = (data.get("agent") or "OPUS").strip()
                if not tid or not content:
                    self._send(400, {"ok": False, "error": "need 'task_id' and 'content'"})
                    return
                path = _write_feedback(tid, agent, content)
                self._send(200, {"ok": True, "feedback_file": path})
            elif self.path == "/fanout":
                data = self._read_json()
                prompts = data.get("prompts")
                if not isinstance(prompts, list) or not prompts:
                    self._send(400, {"ok": False, "error": "need non-empty 'prompts' list"})
                    return
                if not all(isinstance(p, str) for p in prompts):
                    self._send(400, {"ok": False, "error": "'prompts' must be strings"})
                    return
                results = self.workers.fanout(
                    prompts,
                    system=data.get("system"),
                    max_tokens=data.get("max_tokens"),
                    temperature=float(data.get("temperature", 0.7)),
                    think=bool(data.get("think", True)),
                )
                self._send(200, {"ok": True, "results": results})
            else:
                self._send(404, {"ok": False, "error": f"no route {self.path}"})
        except Exception as e:  # noqa: BLE001
            self._send(500, {"ok": False, "error": repr(e)})


# --------------------------------------------------------------------------- #
# Entry point.
# --------------------------------------------------------------------------- #
def serve(host: str = "0.0.0.0", port: int = 8100,
          config_path: Optional[str] = None) -> None:
    agent, cfg, cfg_path, workdir = bootstrap(config_path)

    # Headless-Capture aktivieren (UI bleibt aus → _UI_APP is None).
    gx10._UI_SINK = _capture_sink

    stop = threading.Event()
    # Feedback-Reconciler (Server-seitig; Launch-Seite no-op weil Autopilot aus).
    rt = threading.Thread(
        target=gx10._reconciler_loop,
        args=(stop, gx10.RECONCILER_INTERVAL),
        daemon=True,
    )
    rt.start()
    # Queue-Consumer: wendet die vom Reconciler eingereihten ADVANCE-Befehle an.
    qt = threading.Thread(target=_queue_consumer, args=(agent, stop), daemon=True)
    qt.start()

    _Handler.agent = agent
    _Handler.cfg = cfg
    fanout_conc = int(os.environ.get("GX10_FANOUT_CONCURRENCY", "8"))
    _Handler.workers = ReasoningWorkers(agent.client, agent.model,
                                        max_concurrency=fanout_conc)
    httpd = ThreadingHTTPServer((host, port), _Handler)

    print(f"  Ironclad Orchestrator-Server", flush=True)
    print(f"  Modell : {agent.model}  |  vLLM {cfg['connection']['base_url']}", flush=True)
    print(f"  WORKDIR: {workdir}", flush=True)
    print(f"  Config : {cfg_path or '— (Code-Defaults)'}", flush=True)
    print(f"  Listen : http://{host}:{port}  "
          f"(GET /health /tasks /pending · POST /chat /chat/stream /feedback /fanout)",
          flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        httpd.shutdown()


def main() -> None:
    import argparse

    p = argparse.ArgumentParser(description="Ironclad headless orchestrator server")
    p.add_argument("--host", default=os.environ.get("GX10_SERVER_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("GX10_SERVER_PORT", "8100")))
    p.add_argument("--config", default=None)
    args = p.parse_args()
    serve(host=args.host, port=args.port, config_path=args.config)


if __name__ == "__main__":
    main()
