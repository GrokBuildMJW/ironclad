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

Endpoints (all JSON; trust model = the configured security.profile — see security.md):
  GET  /health        → liveness + effective config summary
  GET  /tasks         → TaskStore snapshot (all statuses)
  GET  /pending       → tasks awaiting a local code-agent (pending + handover present)
  GET  /coders        → which coding agents are bound (registry + boot probe) + the fan-out lane
  GET  /doctor        → runtime ACK/registry self-check (read-only)
  GET  /catalogue     → loaded prompt/skill registry snapshot (for client autocomplete)
  POST /chat          → ``{"message": str}`` → run one orchestrator turn, return captured output
  POST /chat/stream   → streamed turn; passes local code-tool calls back over the wire as
                        ``\x00TR\x00`` frames (+ ``\x00HB\x00`` heartbeats) for the client tool-bridge
  POST /tool-result   → ``{"id","result"}`` → the client returns a passed-through code-tool result
  POST /feedback      → ``{"task_id","agent","content"}`` → drop the feedback file the reconciler advances on
  POST /coders        → ``{"agent": <id>|"auto"|null}`` → pin/clear the runtime code-agent (#454)
  POST /cancel        → set the engine cancel event; the running turn aborts at its next iteration
  POST /fanout        → ``{"prompts":[...], "system"?, "max_tokens"?, "temperature"?, "think"?}`` → run
                        independent reasoning prompts CONCURRENTLY against the local model; input order.
                        Stateless — does not take the agent lock.
  POST /session/open|heartbeat|close → Phase-d session lifecycle (gated profiles; see security.md)

Secret-free: imports only :mod:`gx10` + stdlib. All connection details come from the
config tree (``conf/…``), never hard-coded here.
"""
from __future__ import annotations

import copy
import json
import os
import secrets
import socket
import sys
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlsplit

#: Hard cap on a request body — bounds per-connection allocation on the threaded server.
_MAX_BODY_BYTES = 8 * 1024 * 1024

# The engine is run as a standalone script directory (gx10.py puts core/ on
# sys.path, not as a package). We mirror that: put this dir (core/engine) on the
# path, then import gx10 absolutely — works both as a script AND as a module.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import gx10  # noqa: E402  (also puts core/ on sys.path → ack importable)
import providers  # noqa: E402  (#455: result classifier; #449/#452 registry helpers)
from workers import ReasoningWorkers  # noqa: E402
from security import SecurityPolicy, SessionRegistry  # noqa: E402
from ack import doctor  # noqa: E402

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
# Client tool bridge — pass code-tools THROUGH to the driving client.
# When a client opts in (header X-Local-Tools: 1) on /chat/stream, the engine routes
# the LOCAL_TOOL_NAMES tools to this bridge: it emits a control frame in the stream
# (``\x00TR\x00{json}\x00``), the client runs the tool on its LOCAL filesystem and POSTs
# /tool-result, and the blocked turn resumes. Turns are agent-lock-serialized, so at most
# one bridge is active → a single module-level holder suffices.
# --------------------------------------------------------------------------- #
_TR_PREFIX = "\x00TR"        # frame = \x00TR{json}\x00 — no internal \x00 (single delimiter)
_TR_SUFFIX = "\x00"
_ACTIVE_BRIDGE: Dict[str, Any] = {"b": None}


class ToolBridge:
    def __init__(self, emit: Any, timeout: float = 180.0) -> None:
        self._emit = emit            # write(str) → the live stream
        self._timeout = timeout
        self._lock = threading.Lock()
        self._pending: Dict[str, Dict[str, Any]] = {}   # id → {event, result}

    def __call__(self, name: str, args: Dict[str, Any]) -> str:
        return self.request(name, args)

    def request(self, name: str, args: Dict[str, Any]) -> str:
        rid = secrets.token_hex(8)
        ev = threading.Event()
        with self._lock:
            self._pending[rid] = {"event": ev, "result": None}
        self._emit(_TR_PREFIX + json.dumps({"id": rid, "name": name, "args": args})
                   + _TR_SUFFIX)
        if not ev.wait(self._timeout):
            with self._lock:
                self._pending.pop(rid, None)
            return f"ERROR: client tool '{name}' timed out after {self._timeout:.0f}s"
        with self._lock:
            slot = self._pending.pop(rid, {})
        return slot.get("result") or ""

    def deliver(self, rid: str, result: str) -> bool:
        with self._lock:
            slot = self._pending.get(rid)
            if not slot:
                return False
            slot["result"] = result
            slot["event"].set()
            return True


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
    # S5b: a PRISTINE snapshot of the deployment base — a /switch re-overlays a project's config from this.
    # Must NOT alias _EFFECTIVE_CFG (which `/config set`, `/coders`, … mutate in place at runtime).
    gx10._BASE_CFG = copy.deepcopy(cfg)
    gx10._load_skills(cfg["paths"].get("plugins_dir"))    # core built-ins (always) + 3rd-party (plugins_dir)
    gx10._CFG_SOURCE = cfg_path

    # Resolve the prompt absolutely before chdir (relative → SCRIPT_DIR), like main().
    prompt_cfg = cfg["paths"]["system_prompt"]
    prompt_abs = ""
    if prompt_cfg:
        pp = Path(prompt_cfg).expanduser()
        prompt_abs = str(pp if pp.is_absolute() else (gx10.SCRIPT_DIR / pp))

    workdir = Path(cfg["paths"]["workdir"]).expanduser().resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    os.chdir(workdir)

    # S5b: bring up the installation-global Project Registry, ensure the implicit `default` project
    # (root == workdir) and bind its ProjectContext on this (boot) thread. Behaviour-preserving — the
    # default project resolves paths to workdir and binds an empty mem_ns (legacy/base memory partition).
    gx10.init_registry(workdir)

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
    if gx10.session_path().exists():
        try:
            agent.load_session()
        except Exception:
            pass

    # Server policy: never launch claude itself (that's the client's job), but keep
    # the feedback reconciler running so posted feedback advances tasks.
    gx10.AUTOPILOT_ENABLED = False
    gx10._WATCHER_ENABLED = True
    return agent, cfg, cfg_path, workdir


# --------------------------------------------------------------------------- #
# Background: feedback-side reconciler + a headless queue consumer.
# The reconciler enqueues structured ADVANCE commands onto gx10._INPUT_QUEUE;
# the consumer applies them (and any plain prompts) under the agent lock.
# --------------------------------------------------------------------------- #
def _queue_consumer(agent: gx10.GX10, stop: threading.Event,
                    sessions: Optional[SessionRegistry] = None) -> None:
    while not stop.is_set():
        try:
            item = gx10._INPUT_QUEUE.get(timeout=1.0)
        except Exception:
            continue
        item = (item or "").strip()
        if not item:
            continue
        if item.startswith(gx10._LAUNCH_CMD):
            # Launching is the client's job — the server starts no code-agents.
            continue
        if item.startswith(gx10._ADVANCE_CMD):
            parts = item.split("\x00")  # ['', 'advance', tid, agent]
            if len(parts) >= 4:
                tid, agent_adv = parts[2], parts[3]
                with _AGENT_LOCK:
                    gx10.bind_active()          # S5b: this daemon thread → the active project's ctx
                    try:
                        res = gx10._advance_pipeline(tid, agent_adv)
                    except Exception as e:  # noqa: BLE001
                        res = f"ERROR: {e!r}"
                print(f"[ADVANCE] {tid} ({agent_adv}): {res.splitlines()[0] if res else res}",
                      flush=True)
                # Autoplan (decoupled from autopilot — launching is the client's job): on
                # empty pipeline, enqueue the next planning turn. Only fires when
                # `/autoplan on` is set and a backlog is configured —
                # AND the channel is not sealed (no client present to execute).
                sealed = sessions.is_sealed() if sessions is not None else False
                if res and res.startswith("OK") and not sealed:
                    gx10._autoplan_tick(tid, lambda p: gx10._INPUT_QUEUE.put(p))
                elif sealed:
                    print("[AUTOPLAN] paused — channel sealed (no live session)", flush=True)
            continue
        # Plain prompt (e.g. autoplan) → normal turn.
        with _AGENT_LOCK:
            gx10.bind_active()                  # S5b: this daemon thread → the active project's ctx
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
    reg = gx10._code_agent_registry()          # #449: config-driven code-agent registry
    out: list[Dict[str, Any]] = []
    for task in store.list("pending"):
        tid = task.get("id") or ""
        ho = gx10._find_handover(tid)
        if not ho:
            continue
        staged = gx10._task_agent(task) or gx10._agent_from_handover(ho.name)
        # #454: operator pin overrides the staged agent. #456: a budget failover stays within the agents
        # capable of the task's class (security/architecture → OPUS, etc.) — the staged pick is unchanged.
        agent = gx10._effective_code_agent(staged, task_class=gx10._task_class(task))
        spec = reg.resolve(agent)
        if spec is None:                       # #449: unknown agent → fail-closed (not dispatchable)
            continue
        # #454 (review B): the staged handover's `to:`/`effort:` frontmatter is the orchestrator's choice
        # for the STAGED agent — honour it only when no pin overrode it. When the operator PINNED a
        # different agent, use THAT agent's own model/effort (else the pinned agent would run the staged
        # agent's model, masking which agent really ran + breaking the per-agent model contract).
        if agent == staged:
            model, effort = gx10._parse_handover_meta(ho)
        else:
            model, effort = None, None
        try:
            content = ho.read_text(encoding="utf-8")
        except OSError:
            content = ""
        out.append({
            "id": tid,
            "agent": agent,
            "title": task.get("title"),
            "type": task.get("type"),
            "handover_file": ho.name,
            "handover": content,
            # #449 (C0R-9): the SERVER resolves the FULL agent spec from the registry; the client is a
            # thin renderer (it runs `bin`+`cmd_template` as sent, no client-side registry). None values
            # let the client fall back byte-identically to its Claude defaults (CLAUDE_BIN/AGENT_CMD).
            "model": model or spec.model,
            "effort": effort or spec.effort,
            "bin": spec.bin,
            "cmd_template": spec.cmd_template,
            "permission": spec.permission_mode,
            # #480: the read-only Memory MCP, gated server-side on the sealed profile + a configured memory
            # service + the agent's mcp_template. The client fills the {mcp} placeholder with `mcp` and sets
            # `mcp_env` on the agent subprocess (the spawned MCP inherits the memory connection). ("",{})
            # under open/token ⇒ the launch is byte-identical to today.
            **dict(zip(("mcp", "mcp_env"), gx10._mcp_for_launch(spec))),
        })
    return out


#: #452 (review A perf): the boot probe (`shutil.which` + glob per agent) is cheap but not free, and
#: /health re-derives the coders count on every 2s poll. Cache the probe for a short TTL so the poll
#: reuses it; agent availability does not change second-to-second, and the full GET /coders stays fresh
#: enough. A benign cross-thread race (two probes, idempotent dict write) is fine here.
_PROBE_CACHE: Dict[str, Any] = {"at": -1e9, "data": None}
_PROBE_TTL_S = 10.0


def _probe_cached() -> Dict[str, Any]:
    from providers import probe_code_agents
    now = time.monotonic()
    data = _PROBE_CACHE["data"]
    if data is None or (now - _PROBE_CACHE["at"]) > _PROBE_TTL_S:
        data = probe_code_agents(gx10._code_agent_registry())
        _PROBE_CACHE["data"] = data
        _PROBE_CACHE["at"] = now
    return data


def _coders_snapshot() -> Dict[str, Any]:
    """#452: which CODING agents are bound (the code-agent registry + the prompt-free boot probe) plus
    the fan-out provider lane (the dispatcher snapshot). Answers '/coders zeigt welche agents aktuell
    aktiv angebunden sind'. Never raises — a probe/dispatcher hiccup degrades to an empty view."""
    coding: list[Dict[str, Any]] = []
    try:
        reg = gx10._code_agent_registry()
        probe = _probe_cached()
        breaker = gx10._breaker_snapshot()                 # #455: budget-exhausted agents (failed over)
        enabled_ids = set(reg.names())
        # #460: show ALL onboarded agents, including a disabled one (e.g. KIMI pending exhausted-signal
        # calibration) — it is inert (not probed/launchable) but visible as registered. enabled-only agents
        # keep their boot-probe binding; a disabled one is enabled:false / bound:false.
        for aid in reg.all_ids():
            is_enabled = aid in enabled_ids
            spec = reg.spec_of(aid)
            path = probe.get(aid) if is_enabled else None
            coding.append({
                "id": aid,
                "display": spec.agent_display() if spec else aid,
                "model": spec.model if spec else None,
                "enabled": is_enabled,                     # #460: False ⇒ onboarded but not yet activated
                "bound": bool(path),                       # bin resolved on this machine = bound/active
                "bin": path,
                "unavailable": aid in breaker,             # #455: breaker-tripped (budget exhausted)
                "unavailable_reason": breaker.get(aid),
            })
    except Exception:
        coding = []
    providers_block = {"active": False, "pool": [], "budget": {"usd_cap": None, "spent_usd": 0.0}}
    try:
        if gx10._DISPATCHER is not None:
            providers_block = gx10._DISPATCHER.snapshot()
    except Exception:
        pass
    # #454: the runtime operator pin (None ⇒ auto = the orchestrator's task-chosen staged agent).
    try:
        pinned = gx10._code_agent_pin()
    except Exception:
        pinned = None
    return {"coding_agents": coding, "providers": providers_block, "pinned": pinned}


def _set_coder_pin(agent: Optional[str]) -> Dict[str, Any]:
    """#454: set/clear the runtime `/coders use <id>` pin. ``None``/``"auto"`` clears it; any other
    value must name a CONFIGURED agent (fail-closed). Mutates the live ``code_agents.pinned`` config."""
    raw = (agent or "").strip().upper()
    if raw in ("", "AUTO", "NONE", "OFF"):
        target = None
    elif gx10._code_agent_registry().has(raw):
        target = raw
        gx10._breaker_reset(raw)   # #455: explicitly choosing an agent clears its budget breaker (recovery)
    else:
        names = ", ".join(gx10._agent_names()) or "none"
        raise ValueError(f"unknown agent {raw!r} (configured: {names})")
    cfg = gx10._EFFECTIVE_CFG
    if cfg is None:
        cfg = gx10._EFFECTIVE_CFG = gx10._code_defaults()
    cfg.setdefault("code_agents", {})["pinned"] = target
    return {"pinned": gx10._code_agent_pin()}


def _coders_health() -> Dict[str, int]:
    """Compact bound/total for the /health 2s poller (the full view is GET /coders). Uses ONLY the
    cached probe + registry names — NOT the full snapshot — so the hot path skips the dispatcher
    snapshot and its per-provider bin resolution (review B S3). Never raises."""
    try:
        names = gx10._code_agent_registry().names()
        probe = _probe_cached()
        return {"bound": sum(1 for a in names if probe.get(a)), "total": len(names)}
    except Exception:
        return {"bound": 0, "total": 0}


def _doctor_report() -> Dict[str, Any]:
    """Runtime ACK contract self-check (read-only) over the active workspace — the same
    preflight the doctor CLI runs, exposed live so contract drift surfaces at runtime
    instead of only via tooling. Includes Lodestar's checks when the plugin is enabled."""
    extra = doctor._load_lodestar_checks(bool(gx10.LODESTAR_ENABLED))
    # B3: the task/handover artifacts live under the active initiative — point the doctor there
    # (fall back to the workdir when no initiative is active, so the read-only check never crashes).
    root = gx10.artifact_root_soft() or Path(os.getcwd())
    report = doctor.run_doctor(root, extra_checks=extra)
    return {
        "ok": not report.has_errors(),
        "errors": report.count(doctor.Severity.ERROR),
        "warnings": report.count(doctor.Severity.WARN),
        "findings": [f.as_dict() for f in report.findings],
    }


def _write_feedback(task_id: str, agent: str, content: str) -> str:
    """Drop ``{task_id}_{AGENT}-feedback.md`` into the active initiative's feedback inbox
    (``<initiative>/.work/feedback``). The server-side reconciler detects it (mtime-stable)
    and advances the task. Fail-closed: requires an active initiative (B3)."""
    d = gx10.feedback_dir()
    d.mkdir(parents=True, exist_ok=True)
    agent_u = (agent or "").upper()            # #449: the /feedback handler validates the agent first
    fb = d / f"{task_id}_{agent_u}-feedback.md"
    fb.write_text(content, encoding="utf-8")
    return str(fb)


class _Handler(BaseHTTPRequestHandler):
    server_version = "Ironclad-Orchestrator/0"

    # The GX10 agent + config + reasoning workers are injected by the server.
    agent: gx10.GX10
    cfg: Dict[str, Any]
    workers: ReasoningWorkers
    # Default to the open profile so the handler is usable even if a harness forgets to
    # inject a policy; serve() overrides both from config.
    policy: SecurityPolicy = SecurityPolicy("open", None, 30, "mount")
    sessions: SessionRegistry = SessionRegistry(policy)

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter, single line
        print(f"[http] {self.address_string()} {fmt % args}", flush=True)

    # ── helpers ──────────────────────────────────────────────
    def _send(self, code: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _guard(self) -> bool:
        """Phase-d gate: deployment-secret + session/seal check for protected routes.
        Returns True if the request may proceed; otherwise sends a 401 and returns False.
        ``open`` profiles pass everything through (GATED_PATHS check is a no-op)."""
        refusal = self.sessions.authorize(
            self.path,
            self.headers.get("Authorization"),
            self.headers.get("X-Session-Id"),
        )
        if refusal is None:
            return True
        self._send(refusal["code"], {"ok": False, "error": refusal["error"]})
        return False

    def _read_json(self) -> Dict[str, Any]:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if n <= 0 or n > _MAX_BODY_BYTES:   # reject absurd/oversized bodies (no huge alloc)
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
            self.path = urlsplit(self.path).path   # gate + route on the query-free path
            if self.path == "/health":
                self._send(200, {
                    "ok": True,
                    "model": self.agent.model,
                    "orchestrator_version": gx10.orchestrator_version(),
                    "base_url": self.cfg["connection"]["base_url"],
                    "workdir": os.getcwd(),
                    "watcher": gx10._WATCHER_ENABLED,
                    "autopilot": gx10.AUTOPILOT_ENABLED,
                    "language": gx10.LANGUAGE,
                    # #385: report the Cold (Mem0) AND Warm (Valkey) tiers SEPARATELY. `memory` was
                    # Cold-only, so a silent Warm outage (Valkey unreachable → fail-soft no-op) read as a
                    # healthy `memory: up` and could regress unnoticed; `warm` surfaces it.
                    "memory": ("off" if gx10._MEMORY is None
                               else ("up" if gx10._MEMORY.is_available() else "down")),
                    "warm": ("off" if gx10._WARM is None
                             else ("up" if gx10._WARM.is_available() else "down")),
                    "security": self.policy.summary(),
                    "sealed": self.sessions.is_sealed(),
                    "coders": _coders_health(),            # #452: compact bound/total for the 2s poller
                    # #601 isolation observability: the active project, the installation-global home, and
                    # whether the registry is wired or fell back to un-isolated mode (else only logged at boot).
                    "registry": gx10.registry_health(),
                })
            elif self.path == "/tasks":
                if not self._guard():
                    return
                gx10.bind_active()          # S5b: this request thread → the active project's ctx
                self._send(200, {"tasks": gx10._store().list()})
            elif self.path == "/pending":
                if not self._guard():
                    return
                gx10.bind_active()          # S5b: this request thread → the active project's ctx
                self._send(200, {"pending": _pending_handovers()})
            elif self.path == "/coders":
                # #452: which coding agents are bound + the fan-out provider lane. Guarded like /tasks.
                if not self._guard():
                    return
                self._send(200, _coders_snapshot())
            elif self.path == "/doctor":
                if not self._guard():
                    return
                gx10.bind_active()          # S5b: this request thread → the active project's ctx
                self._send(200, _doctor_report())
            elif self.path == "/catalogue":
                # Read-only snapshot of the loaded prompt/skill registry — the source the
                # TypeScript client merges into slash autocomplete (#149). Same `_catalogue_snapshot`
                # that backs the `/prompts`/`/skills` commands (one surface, no re-scan).
                if not self._guard():
                    return
                self._send(200, gx10._catalogue_snapshot())
            else:
                self._send(404, {"ok": False, "error": f"no route {self.path}"})
        except Exception as e:  # noqa: BLE001
            self._send(500, {"ok": False, "error": repr(e)})

    def do_POST(self) -> None:
        try:
            self.path = urlsplit(self.path).path   # gate + route on the query-free path
            # Session lifecycle (Phase d). /session/open needs only the deployment
            # secret (no session yet); heartbeat/close just touch the registry.
            if self.path == "/session/open":
                if not self.policy.check_token(self.headers.get("Authorization")):
                    self._send(401, {"ok": False, "error": "missing or invalid deployment secret"})
                    return
                self._send(200, {"ok": True, **self.sessions.open()})
                return
            if self.path == "/session/heartbeat":
                sid = (self._read_json().get("session_id")
                       or self.headers.get("X-Session-Id") or "")
                ok = self.sessions.heartbeat(sid)
                self._send(200 if ok else 410, {"ok": ok})
                return
            if self.path == "/session/close":
                sid = (self._read_json().get("session_id")
                       or self.headers.get("X-Session-Id") or "")
                self._send(200, {"ok": True, "closed": self.sessions.close(sid)})
                return

            if not self._guard():
                return

            if self.path == "/chat":
                data = self._read_json()
                message = (data.get("message") or "").strip()
                if not message:
                    self._send(400, {"ok": False, "error": "missing 'message'"})
                    return
                with _Captured() as cap:
                    with _AGENT_LOCK:
                        gx10.bind_active()      # S5b: this request thread → the active project's ctx
                        gx10._dispatch(self.agent, message)
                self._send(200, {"ok": True, "output": cap.text})
            elif self.path == "/chat/stream":
                data = self._read_json()
                message = (data.get("message") or "").strip()
                if not message:
                    self._send(400, {"ok": False, "error": "missing 'message'"})
                    return
                # Live: no Content-Length, Connection: close → the client reads until
                # EOF. Every _ui_print chunk is flushed to the socket immediately.
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                # Disable Nagle: a small flushed frame (e.g. a tool-call passthrough) must
                # reach the client IMMEDIATELY, not wait for more data — otherwise the
                # bridge round-trip deadlocks and live streaming stutters.
                try:
                    self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                except OSError:
                    pass

                # Serialize all writes: a heartbeat frame must never interleave INSIDE a
                # text chunk or a \x00TR…\x00 tool frame (a stray \x00 would split the
                # frame and desync the client parser).
                _write_lock = threading.Lock()

                def _write(text: str) -> None:
                    with _write_lock:
                        try:
                            self.wfile.write(text.encode("utf-8", "replace"))
                            self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError, OSError):
                            pass  # client gone → the turn finishes server-side
                # Opt-in: if the client offers local execution (X-Local-Tools: 1), pass
                # code-tools through to it; otherwise they run server-side as before.
                local = self.headers.get("X-Local-Tools") == "1"
                bridge = ToolBridge(_write) if local else None
                # Keep-alive: a turn can run for minutes with NO output (e.g. a reasoning
                # panel computing perspectives). Without bytes on the wire the client's HTTP
                # body stream idles into a timeout ("TypeError: terminated") and the result —
                # though finished server-side — never reaches the live view. So while the turn
                # runs we emit a no-op frame \x00HB\x00 every few seconds; the client parser
                # sees a control frame with no valid TR-JSON and silently drops it (it never
                # appears in the text). Stops as soon as _dispatch returns.
                _hb_stop = threading.Event()

                def _heartbeat() -> None:
                    while not _hb_stop.wait(10.0):
                        _write("\x00HB\x00")
                hb_thread = threading.Thread(target=_heartbeat, daemon=True)
                hb_thread.start()
                try:
                    with _Streamed(_write):
                        with _AGENT_LOCK:
                            gx10.bind_active()  # S5b: this request thread → the active project's ctx
                            if bridge is not None:
                                _ACTIVE_BRIDGE["b"] = bridge
                                gx10._LOCAL_TOOL_BRIDGE = bridge
                            try:
                                gx10._dispatch(self.agent, message)
                            finally:
                                gx10._LOCAL_TOOL_BRIDGE = None
                                _ACTIVE_BRIDGE["b"] = None
                finally:
                    _hb_stop.set()
                    hb_thread.join(timeout=1.0)
            elif self.path == "/tool-result":
                # The client returns the result of a passed-through code-tool.
                data = self._read_json()
                rid = (data.get("id") or "").strip()
                bridge = _ACTIVE_BRIDGE["b"]
                ok = bool(rid) and bridge is not None and bridge.deliver(rid, data.get("result") or "")
                self._send(200 if ok else 410, {"ok": ok})
            elif self.path == "/cancel":
                # Aborts the currently running turn: the engine _CANCEL_EVENT is
                # set; run() checks it per iteration/generation and stops cleanly.
                # No agent lock needed — the event is thread-safe and the running
                # turn thread polls it. The next turn clears it on start.
                gx10._CANCEL_EVENT.set()
                self._send(200, {"ok": True, "cancelled": True})
            elif self.path == "/feedback":
                gx10.bind_active()          # S5b: this request thread → the active project's ctx
                data = self._read_json()
                tid = (data.get("task_id") or "").strip()
                content = data.get("content") or ""
                agent = (data.get("agent") or "").strip().upper()
                # #455: content may be EMPTY now — a no-feedback run still reports its raw signal
                # (exit_code + stderr) so the server can classify it. Require only task_id + agent.
                if not tid:
                    self._send(400, {"ok": False, "error": "need 'task_id'"})
                    return
                # #449 (review B-6): fail-closed at the boundary — validate the agent against the
                # config-driven registry instead of silently defaulting an unknown/missing one to OPUS.
                if not gx10._code_agent_registry().has(agent):
                    self._send(400, {"ok": False,
                                     "error": f"unknown agent {agent!r} (configured: "
                                              f"{', '.join(gx10._code_agent_registry().names()) or 'none'})"})
                    return
                # #455: classify the raw run signal (layered JSON→stderr→exit; conf patterns) →
                # budget-exhausted ⇒ trip the breaker (the next /pending fails over to a peer); a
                # plain failure ⇒ no feedback written (retry); a real result ⇒ advance.
                patterns = ((gx10._EFFECTIVE_CFG or {}).get("code_agents") or {}).get("exhausted")
                cls = providers.classify_agent_result(
                    exit_code=data.get("exit_code"), stderr=data.get("stderr") or "",
                    has_feedback=bool(content.strip()), patterns=patterns)
                # #602 2.4/#805: opt-in (strategy.enabled) — classify WHY the run failed into the shared
                # FailureClass + record it for the Strategy consumer (2.5/#806); None + no field when off.
                fc = gx10._record_failure_class(cls)
                # #602 2.5/#806: per-task Strategy on the run result — HUMAN_ESCALATION when the attempt
                # budget is spent (instead of an endless silent failover); a success (OK) resets the counter.
                strat = gx10._revise_on_failure(tid, cls)
                if cls == providers.RESULT_UNAVAILABLE:
                    gx10._breaker_trip(agent, "budget/quota exhausted")
                    self._send(200, {"ok": True, "classification": cls,
                                     "action": "breaker-tripped → failover on the next poll",
                                     **({"failure_class": fc} if fc else {}),
                                     **({"strategy": strat} if strat else {})})
                    return
                if cls == providers.RESULT_FAILED:
                    self._send(200, {"ok": True, "classification": cls, "action": "no-feedback",
                                     **({"failure_class": fc} if fc else {}),
                                     **({"strategy": strat} if strat else {})})
                    return
                path = _write_feedback(tid, agent, content)
                self._send(200, {"ok": True, "classification": cls, "feedback_file": path})
            elif self.path == "/coders":
                # #454: `/coders use <id>` pins the coding agent at runtime (`auto`/null clears it).
                data = self._read_json()
                try:
                    res = _set_coder_pin(data.get("agent"))
                except ValueError as e:
                    self._send(400, {"ok": False, "error": str(e)})
                    return
                self._send(200, {"ok": True, **res})
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
    # UTF-8 stdout + color decision. Headless server stdout is not a TTY → color is
    # dropped, so no ANSI escape codes leak into the captured /chat HTTP payload.
    gx10._setup_output()
    agent, cfg, cfg_path, workdir = bootstrap(config_path)

    # Phase-d trust policy (single-tenant). Fail-closed: a profile that demands a
    # deployment secret refuses to boot without one. ``sealed`` forces a loopback bind.
    policy = SecurityPolicy.from_config(cfg)
    err = policy.startup_error()
    if err:
        print(f"  [SECURITY] refusing to start: {err}", flush=True)
        raise SystemExit(2)
    sessions = SessionRegistry(policy)
    host = policy.effective_bind(host)

    # Enable headless capture (UI stays off → _UI_APP is None).
    gx10._UI_SINK = _capture_sink

    stop = threading.Event()
    # Feedback reconciler (server-side; launch side is a no-op because autopilot is off).
    rt = threading.Thread(
        target=gx10._reconciler_loop,
        args=(stop, gx10.RECONCILER_INTERVAL),
        daemon=True,
    )
    rt.start()
    # Queue consumer: applies the ADVANCE commands enqueued by the reconciler;
    # the registry drives the autoplan pause when the channel is sealed.
    qt = threading.Thread(target=_queue_consumer, args=(agent, stop, sessions), daemon=True)
    qt.start()

    _Handler.agent = agent
    _Handler.cfg = cfg
    _Handler.policy = policy
    _Handler.sessions = sessions
    # Phase-e reasoning fan-out governor — config-driven, model-matched in conf/.
    wcfg = cfg.get("workers") or {}
    _Handler.workers = ReasoningWorkers(
        agent.client, agent.model,
        max_concurrency=int(wcfg.get("concurrency", 4)),
        default_max_tokens=int(wcfg.get("max_tokens", 1024)),
        max_batch_tokens=int(wcfg.get("max_batch_tokens", 8192)),
    )
    gx10._WORKERS = _Handler.workers   # shared handle for the in-engine parallel tool
    # P0 provider router (beside _WORKERS). The boot-fixed `setup.type` (docs/setup-types.md) drives the
    # runner wiring: server → dispatcher inactive (in-engine only, byte-identical); local → local-subprocess
    # runner (engine + agents co-located on the desktop). Orchestrator + agents are always co-located —
    # no cross-machine offload. The dispatcher code is unchanged; only WHICH runner closure we inject differs.
    pcfg = cfg.get("providers") or {}
    # #451: per-enabled-agent boot probe (prompt-free path resolution) instead of a single
    # which(CLAUDE_BIN). cli-available = AT LEAST ONE enabled code-agent resolves; fail-closed only
    # when ZERO resolve. Each agent's bin is resolved via PATH (a shim) or its private-layer bin_glob.
    try:
        from providers import probe_code_agents
        _probe = probe_code_agents(gx10._code_agent_registry())
        for _aid, _path in _probe.items():
            print(f"  [agents] {_aid}: {'resolved → ' + _path if _path else 'NOT resolved'}", flush=True)
        _cli_ok = any(_probe.values())
        if not _cli_ok:
            print("  [agents] WARNING: no code-agent binary resolved — the local handover lane is "
                  "unavailable (set GX10_CLAUDE_BIN / a code_agents bin or bin_glob in conf/).", flush=True)
    except Exception as _pe:
        print(f"  [agents] probe failed ({_pe!r}) — assuming no local agent", flush=True)
        _cli_ok = False
    try:
        topo = gx10.resolve_offload_topology(cfg, cli_available=_cli_ok)   # FAIL-CLOSED on bad topology
    except ValueError as e:
        print(f"  [setup] FATAL: {e}", flush=True)
        raise SystemExit(2)
    if topo.get("note"):
        print(f"  [setup] {topo['note']}", flush=True)
    try:
        from providers import load_registry
        from dispatch import ProviderDispatcher
        reg = load_registry(cfg)                          # None ⇒ no pool → dispatcher inactive
        _runner = None
        if topo["runner_mode"] == "local":               # local: offload = local subprocess CLI (co-located)
            from client import default_cli_runner
            _runner = (lambda spec, prompt, **kw:
                       default_cli_runner(spec, prompt, timeout=pcfg.get("cli_timeout_s"), **kw))
        # runner_mode == "none" → server: no runner, dispatcher stays on in-engine fanout.
        gx10._DISPATCHER = ProviderDispatcher(
            reg, workers=_Handler.workers, agent_runner=_runner,
            enabled=bool(topo["providers_enabled"]),       # derived from setup.type (single source)
            effort_max_tokens=pcfg.get("effort_max_tokens"),
            max_agents=int(pcfg.get("max_agents", 3)),     # providers.max_agents (server cap, ≠ --max-agents)
        )
        # epic #505 S3: the standalone web-search adapter seam, selected from the `search` config
        # block (cli / brave / mock) independent of the dispatcher registry. runner_mode gates the
        # native brave adapter to a local setup (Fork 2); server mode falls back to the CLI lane.
        from websearch_adapters import build_web_search_adapter
        gx10._WEBSEARCH = build_web_search_adapter(cfg, gx10._DISPATCHER, runner_mode=topo["runner_mode"])
        # epic #505 S8: boot-time visibility. Web search stays OFF (fail-soft) rather than blocking
        # boot when its adapter is unusable (e.g. the native adapter on a local setup with no key) —
        # the fail-closed posture of SecurityPolicy, minus refusing to boot (search is optional).
        _scfg = (cfg or {}).get("search") or {}
        if _scfg.get("enabled", True) and gx10._WEBSEARCH is not None and not gx10._WEBSEARCH.available():
            print(f"  [search] web_search OFF — adapter {_scfg.get('adapter', 'cli')!r} is not usable "
                  f"(for the native adapter on a local setup, set ${_scfg.get('api_key_env', 'GX10_SEARCH_API_KEY')}).",
                  flush=True)
    except SystemExit:
        raise
    except Exception:
        gx10._DISPATCHER = None                            # fail-soft: any wiring error → today's path
        gx10._WEBSEARCH = None
    httpd = ThreadingHTTPServer((host, port), _Handler)

    print(f"  Ironclad Orchestrator-Server  (version {gx10.orchestrator_version()})", flush=True)
    print(f"  Model  : {agent.model}  |  vLLM {cfg['connection']['base_url']}", flush=True)
    print(f"  WORKDIR: {workdir}", flush=True)
    print(f"  Config : {cfg_path or '— (Code-Defaults)'}", flush=True)
    sec = policy.summary()
    print(f"  Security: profile={sec['profile']}  auth={sec['auth']}  "
          f"session={sec['session']}  code={sec['code_locality']}", flush=True)
    # Runtime ACK contract self-check at boot — fail-loud-ish (log only, never blocks).
    try:
        rep = _doctor_report()
        tail = (" (clean)" if not rep["errors"] and not rep["warnings"]
                else f" — details: GET /doctor")
        print(f"  Doctor : {rep['errors']} error(s), {rep['warnings']} warning(s){tail}",
              flush=True)
    except Exception as e:  # noqa: BLE001 — diagnostics must never block startup
        print(f"  Doctor : self-check skipped ({e!r})", flush=True)
    print(f"  Listen : http://{host}:{port}  "
          f"(GET /health /tasks /pending /coders /doctor · POST /chat /chat/stream /coders /cancel "
          f"/feedback /fanout /session/open|heartbeat|close)",
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
