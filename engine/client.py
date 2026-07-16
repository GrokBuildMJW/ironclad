"""Thin orchestrator client — the PC side of the server/client split.

> **LEGACY / DEPRECATED — superseded by the TypeScript terminal client in
> ``clients/ink/`` (the recommended interactive UI; see the top-level README / SETUP.md).**
> This Python REPL is kept as a **zero-dependency reference and headless fallback** (no
> Node required) and is still maintained, but it is no longer the primary client.

> **Connects exactly like the CLI connects to the model: plain LAN HTTP, this side
> initiates.** The orchestrator (reasoning + state) lives on the server
> (:mod:`engine.server`, on the Spark). This client holds nothing but the
> conversation REPL and the *code locality*: project code stays on this machine, and
> the code-agents (``claude --print``) run HERE, against the local working copy —
> never on the server.

The loop, end to end:
  1. You type → ``POST /chat`` → the server runs one orchestrator turn → its output
     prints here. ``stage_handover`` on the server creates handover files server-side.
  2. ``/pending`` (or the ``/auto`` poller) pulls handovers the server has staged.
  3. For each, the client writes the handover into a hidden LOCAL scratch dir
     (``.ironclad/agent/handovers/``) and runs ``claude --print`` with the local code root as
     cwd — so the code-agent edits *local* code, reading ``.claude/CLAUDE.md`` like a normal session.
  4. claude writes ``.ironclad/agent/feedback/{id}_{AGENT}-feedback.md`` locally; the client
     uploads it via ``POST /feedback``; the server's reconciler advances the task. (The scratch
     dir is HTTP-mediated, hence independent of the server-side initiative routing — and kept out
     of the project root.)

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
import shlex
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import config_schema
import proc_tree
from commands import HELP_TEXT, build_agent_argv, classify, setup_output

# Default is localhost (secret-free); the real server address (e.g. the box on the
# LAN) comes from GX10_SERVER_URL / --server — private via conf/, never hard-coded here.
DEFAULT_SERVER = os.environ.get("GX10_SERVER_URL", "http://localhost:8100")
#: Optional deployment secret (Phase d, profiles token/sealed). NOT a user login —
#: see docs/roadmap.md. Empty → no Authorization header is sent.
SERVER_TOKEN = os.environ.get("GX10_SERVER_TOKEN") or None
#: Optional client-managed transport (Phase d, sealed). A full command that opens a
#: forward so the server is reachable at GX10_SERVER_URL (e.g. an SSH local-forward).
#: Generic on purpose: the SSH/host specifics live in the operator's private config,
#: never here. The client runs it as a child and tears it down on exit.
TUNNEL_CMD = os.environ.get("GX10_TUNNEL_CMD") or None
CLAUDE_BIN = os.environ.get("GX10_CLAUDE_BIN", "claude")
DEFAULT_EFFORT = os.environ.get("GX10_CLAUDE_EFFORT", "high")
#: Safe fallback for an old server item that omits the per-agent permission mode. A bypass
#: additionally requires the server-shipped per-agent capability below.
_SAFE_PERMISSION_MODE = config_schema.defaults_tree()["code_agents"]["pool"][0]["permission_mode"]
CLAUDE_PERMISSION_MODE = os.environ.get("GX10_CLAUDE_PERMISSION_MODE", _SAFE_PERMISSION_MODE)
#: The command that runs a local code-agent on a handover — a TEMPLATE so ANY headless
#: coding CLI (not only Claude Code) can be wired with **no code change**. Placeholders:
#: ``{bin} {model} {effort} {permission} {prompt}`` (use only the ones your CLI needs;
#: ``{prompt}`` must be its own token so it stays a single argument). The default is
#: Claude Code's shape, so nothing changes unless you set ``GX10_AGENT_CMD``. The agent
#: only has to run **headless**, be able to **write files**, and follow the handover
#: prompt (read the handover, do the task, write the feedback file). See docs/code-agents.md.
DEFAULT_AGENT_CMD = ("{bin} --model {model} --effort {effort} "
                     "--permission-mode {permission} --print {prompt}")
_CODER_TIMEOUT_DEFAULT = 1800.0
_TOOL_RESULT_POST_BACKOFF_S = 0.5
_TOOL_RESULT_POST_DEADLINE_S = float(os.environ.get("GX10_TOOL_RESULT_DEADLINE_S") or 150.0)
_TOOL_RESULT_POST_MAX_BACKOFF_S = 5.0
#: Cap on the coder-controlled feedback / capture result file. Feedback is a short summary; the file is
#: read cap+1 bytes at a time so a coder that writes a multi-GB file can never OOM the client. Env: GX10_FEEDBACK_MAX_BYTES.
_FEEDBACK_MAX_BYTES = int(os.environ.get("GX10_FEEDBACK_MAX_BYTES") or 1024 * 1024)
#: #449 (review B round 4): the RAW client-side overrides (None when unset) — an EXPLICIT
#: ``GX10_AGENT_CMD``/``GX10_CLAUDE_BIN`` is the documented single-agent BYO path and must WIN over the
#: server-resolved registry spec (otherwise the default server's OPUS/SONNET template would make the
#: documented client-side override unreachable). When unset, the server spec is authoritative.
AGENT_CMD_OVERRIDE = os.environ.get("GX10_AGENT_CMD") or None
CLAUDE_BIN_OVERRIDE = os.environ.get("GX10_CLAUDE_BIN") or None
AGENT_CMD = AGENT_CMD_OVERRIDE or DEFAULT_AGENT_CMD
#: Max local code-agents running at once. Each is heavy → conservative default; override
#: with GX10_MAX_AGENTS.
DEFAULT_MAX_AGENTS = int(os.environ.get("GX10_MAX_AGENTS", "3"))
#: #455: how much of a code-agent's stderr to upload for the server-side exhausted classifier (a
#: bounded tail — the budget/quota signal is at the end; never ship an unbounded log over the wire).
def _strip_confirm(message: str) -> Tuple[str, bool]:
    """#935/#1281: `--yes`/`--confirm` is the operator's confirmation for a destructive command — a standalone
    token in ANY position (not only trailing; `--yes --purge` used to slip past the old endswith check).
    Returns (message without the flag, whether it was present)."""
    toks = message.rstrip().split()
    kept = [t for t in toks if t not in ("--yes", "--confirm")]
    if len(kept) != len(toks):
        return " ".join(kept), True
    return message.rstrip(), False


_STDERR_TAIL_CHARS = 4000
_MAX_CAPTURE_BYTES = 256 * 1024
_TRUNCATED_MARKER = "…(truncated)…"


class _BoundedTail:
    """Retain only the newest byte-accurate slice of a subprocess stream."""

    def __init__(self) -> None:
        self._tail = b""
        self._truncated = False

    def append(self, chunk: bytes) -> None:
        if len(chunk) >= _MAX_CAPTURE_BYTES:
            self._tail = chunk[-_MAX_CAPTURE_BYTES:]
            self._truncated = True
            return
        overflow = len(self._tail) + len(chunk) - _MAX_CAPTURE_BYTES
        if overflow > 0:
            self._tail = self._tail[overflow:] + chunk
            self._truncated = True
        else:
            self._tail += chunk

    def text(self) -> str:
        retained = (_TRUNCATED_MARKER.encode("utf-8") + self._tail
                    if self._truncated else self._tail)
        return retained.decode("utf-8", errors="replace")


def _drain_stderr(pipe, tail: _BoundedTail) -> None:
    """Continuously drain a binary stderr pipe into a bounded rolling tail."""
    try:
        while True:
            chunk = pipe.read(65536)
            if not chunk:
                break
            tail.append(chunk)
    except (ValueError, OSError):
        pass  # A concurrent close / broken pipe (EIO) during teardown is already a completed drain — the
              # daemon reader must never raise (it would print a stray traceback to the process stderr).
    finally:
        try:
            pipe.close()
        except (ValueError, OSError):
            pass


#: CLI-3 (#503): serializes the check-then-claim in dispatch_pending so an overlapping /auto poll + /work
#: (or two poll ticks) can't both claim+launch the same handover.
_CLAIM_LOCK = threading.Lock()
# Client-run claim lease renewal; the server's default lease TTL is 120 seconds.
_CLAIM_RENEW_INTERVAL_S = 30.0
# #449: the client-side OPUS/SONNET→model table is retired. The server now resolves the agent's
# full spec (bin/cmd_template/model/effort/permission) from the config-driven registry and ships it
# in the /pending item; this client only renders what it is sent (see _run_handover).

# --------------------------------------------------------------------------- #
# Inline presentation — colour the REPL like a real CLI (Claude-Code-style),
# while staying line-based so the *terminal* keeps native scroll + copy + paste.
# No deps: raw ANSI, enabled by setup_output()'s VT switch on Windows.
# --------------------------------------------------------------------------- #
_COLOR = (os.environ.get("NO_COLOR") is None
          and os.environ.get("TERM") != "dumb"
          and sys.stdout.isatty())
_A = {
    "reset": "\x1b[0m", "bold": "\x1b[1m", "dim": "\x1b[2m",
    "cyan": "\x1b[36m", "blue": "\x1b[34m", "green": "\x1b[32m",
    "gray": "\x1b[90m", "yellow": "\x1b[33m", "red": "\x1b[31m",
    "bcyan": "\x1b[96m", "bblue": "\x1b[94m",
}


def _c(text: str, *names: str) -> str:
    """Wrap *text* in the named ANSI styles (no-op when colour is off)."""
    if not _COLOR or not names:
        return text
    return "".join(_A[n] for n in names) + text + _A["reset"]


def _print_banner(srv: "Server", h: Dict[str, Any], codedir: Path, max_agents: int,
                  reachable: bool) -> None:
    """A compact, coloured header — the 'pretty' the full-screen TUI gave up native
    scroll/copy for, here printed inline so the terminal keeps doing both."""
    line = "─" * 52
    dot = _c("●", "green") if reachable else _c("○", "red")
    print(_c("╭" + line + "╮", "bblue"))
    print(_c("│", "bblue") + "  " + _c("◆ Ironclad", "bcyan", "bold")
          + _c("  ·  Orchestrator Client", "gray") + " " * 18 + _c("│", "bblue"))
    print(_c("╰" + line + "╯", "bblue"))
    model = h.get("model", "?") if reachable else "—"
    print(f"  {dot} {_c('server', 'gray')}  {srv.base}   {_c('·', 'gray')}   "
          f"{_c('model', 'gray')} {_c(str(model), 'cyan')}")
    print(f"    {_c('code', 'gray')}  {codedir}   {_c('·', 'gray')}   "
          f"{_c('≤' + str(max_agents) + ' parallel agents', 'gray')}")
    print(_c("    /help · commands   ·   exit · quit   ·   Strg+C · cancel turn", "gray"))


# epic #1043 / #1052: the CLI-runner lane spawns a coder that reads UNTRUSTED content (web results, and —
# with read_offload — local files). It must never inherit the server's secrets, and the ambient push
# credential must be unreachable on the default git/gh path — else a prompt-injection becomes an exfil.
# Harden the child env at this ONE choke point (covers web_search, parallel_reason, and future
# read_offload). Built once + cached (the process env is stable); fail-CLOSED to a plain secret scrub if
# the credential redirect can't be written. See engine/agent_env.py for the invariants (HOME kept, no
# CLAUDE_CONFIG_DIR).
_HARDENED_CHILD_ENV: Optional[Dict[str, str]] = None


def _hardened_child_env() -> Dict[str, str]:
    global _HARDENED_CHILD_ENV
    if _HARDENED_CHILD_ENV is None:
        from agent_env import agent_env_scratch, harden_agent_env, scrub_agent_env
        base = dict(os.environ)
        try:
            hardened = harden_agent_env(base, agent_env_scratch())
        except Exception:  # noqa: BLE001 — fail CLOSED: still remove secrets even if the redirect fails
            hardened = scrub_agent_env(base)
        hardened["PYTHONIOENCODING"] = "utf-8"
        _HARDENED_CHILD_ENV = hardened
    return _HARDENED_CHILD_ENV


def default_cli_runner(spec, prompt: str, *, effort: str, max_tokens: Optional[int] = None,
                       timeout: Optional[float] = None) -> Dict[str, Any]:
    """CLI substrate for the provider dispatcher (MPR P0 §5.2) — CLIENT-lane.

    Spawns a headless code-CLI for one reasoning perspective against the local model CLIs/subscriptions
    on this machine (Sonnet/Kimi/Opus/…), keeping the Spark free. Lives here (not in the server/the
    pure dispatcher) because it owns the subprocess; the server only *injects* this callable.
    Returns the same result shape as ``workers._one`` so aggregation is uniform. Never raises.
    ``permission_mode`` has one source: ``spec.permission_mode`` or ``CLAUDE_PERMISSION_MODE``.
    """
    try:
        from providers import canonical_launch_tuple
        bin_, template = canonical_launch_tuple(spec)
    except Exception:
        template = getattr(spec, "cmd_template", None) or AGENT_CMD
        bin_ = getattr(spec, "bin", None) or CLAUDE_BIN
    from tooling_envelope_runtime import _envelope_authorize
    refused = _envelope_authorize(bin_, template)
    if refused:
        return {"ok": False, "content": None, "error": refused,
                "completion_tokens": None, "latency": 0.0, "tooling_envelope_refused": True}
    permission = getattr(spec, "permission_mode", None) or CLAUDE_PERMISSION_MODE
    bypass_allowed = bool(getattr(getattr(spec, "capabilities", None), "permission_bypass", False))
    bypass_requested = permission == "bypassPermissions" or "--dangerously-skip-permissions" in template
    if bypass_requested and not bypass_allowed:
        refusal = "permission bypass requires capabilities.permission_bypass=true on this agent"
        return {"ok": False, "content": None, "error": refusal,
                "completion_tokens": None, "latency": 0.0, "permission_refused": True}
    argv = build_agent_argv(
        template,
        bin=bin_,
        model=spec.model,
        effort=str(effort),
        permission=permission,
        prompt=prompt,
    )
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            argv, env=_hardened_child_env(),
            stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=timeout,
        )
        return {
            "ok": proc.returncode == 0,
            "content": proc.stdout,
            "error": (proc.stderr or None) if proc.returncode else None,
            "completion_tokens": None,
            "latency": round(time.monotonic() - t0, 3),
        }
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        return {"ok": False, "content": None, "error": repr(e),
                "completion_tokens": None, "latency": round(time.monotonic() - t0, 3)}


# --------------------------------------------------------------------------- #
# HTTP (stdlib).
# --------------------------------------------------------------------------- #
class Server:
    def __init__(self, base_url: str, timeout: float = 600.0,
                 token: Optional[str] = None) -> None:
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.token = token
        self.session_id: Optional[str] = None

    def _headers(self) -> Dict[str, str]:
        h: Dict[str, str] = {}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        if self.session_id:
            h["X-Session-Id"] = self.session_id
        return h

    def _req(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        for k, v in self._headers().items():
            req.add_header(k, v)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}

    def health(self) -> Dict[str, Any]:
        return self._req("GET", "/health")

    def doctor(self) -> Dict[str, Any]:
        return self._req("GET", "/doctor")   # DOCTOR (#503): gated read-only preflight report

    # ── session lifecycle (Phase d; no-op transport-wise on the open profile) ──
    def session_open(self) -> Dict[str, Any]:
        res = self._req("POST", "/session/open", {})
        self.session_id = res.get("session_id")
        return res

    def session_heartbeat(self) -> bool:
        if not self.session_id:
            return False
        try:
            return bool(self._req("POST", "/session/heartbeat",
                                  {"session_id": self.session_id}).get("ok"))
        except urllib.error.URLError:
            return False

    def session_close(self) -> None:
        if not self.session_id:
            return
        try:
            self._req("POST", "/session/close", {"session_id": self.session_id})
        except urllib.error.URLError:
            pass
        self.session_id = None

    def chat(self, message: str) -> Dict[str, Any]:
        return self._req("POST", "/chat", {"message": message})

    def chat_stream(self, message: str, on_text, confirm: bool = False):
        """Stream a turn from /chat/stream, calling ``on_text(chunk)`` as text arrives.
        Code-tools the orchestrator passes through (``\\x00TR{json}\\x00`` frames) are run
        LOCALLY here and their result posted back to /tool-result, so the remote agent
        operates on YOUR filesystem. Decodes UTF-8 incrementally; blocks until done.

        #935: for a destructive command the server replies with a JSON ``{needs_confirm}`` (Content-Type
        application/json) INSTEAD of a stream — this returns that dict (nothing streamed) so the caller can
        confirm and re-call with ``confirm=True``. A normal turn streams and returns ``None``."""
        import codecs
        # #935: uniform confirm affordance — a trailing `--yes`/`--confirm` on a destructive command is the
        # confirmation (stripped here, sent as confirm=True). Keeps every client's flow input-free: on a
        # needs_confirm reply the caller just tells the user to re-run with --yes.
        # #935/#1281: `--yes`/`--confirm` (in ANY position) is the destructive-command confirmation.
        message, _confirmed = _strip_confirm(message)
        confirm = confirm or _confirmed
        body = json.dumps({"message": message, "confirm": confirm}).encode("utf-8")
        req = urllib.request.Request(self.base + "/chat/stream", data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("X-Local-Tools", "1")          # opt in: pass code-tools through to us
        for k, v in self._headers().items():
            req.add_header(k, v)
        dec = codecs.getincrementaldecoder("utf-8")("replace")
        buf = ""
        expecting_frame = False                       # toggles on every \x00 (text↔frame)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            # #935: a destructive command → JSON needs_confirm (not a stream); return it to the caller.
            if (resp.headers.get_content_type() == "application/json"):
                try:
                    return json.loads(resp.read().decode("utf-8", "replace"))
                except Exception:  # noqa: BLE001 — malformed → fall through to normal (no confirm)
                    return None
            # read1: return data from ONE underlying read, don't block for a full buffer —
            # essential for streaming (a small tool-call frame must surface immediately,
            # not wait for 256 bytes / EOF).
            reader = resp.read1 if hasattr(resp, "read1") else resp.read
            while True:
                chunk = reader(256)
                if not chunk:
                    break
                buf += dec.decode(chunk)
                while "\x00" in buf:
                    seg, buf = buf.split("\x00", 1)
                    if expecting_frame:
                        self._run_passthrough_tool(seg)   # seg = "TR{json}"
                    elif seg:
                        on_text(seg)
                    expecting_frame = not expecting_frame
        buf += dec.decode(b"", final=True)
        if buf and not expecting_frame:
            on_text(buf)

    def _run_passthrough_tool(self, frame: str) -> None:
        """Execute a passed-through code-tool LOCALLY and post the result to the server.
        ``frame`` is ``TR{json}`` with the json carrying id/name/args. Results are resent with
        capped backoff, reopening an expired session, until delivered, stale-dropped on 410,
        or the result-post deadline expires."""
        try:
            payload = json.loads(frame[2:]) if frame.startswith("TR") else json.loads(frame)
            rid, name, args = payload["id"], payload["name"], payload.get("args") or {}
        except (ValueError, KeyError):
            return
        try:
            import gx10  # importable without openai; run_tool acts on the local cwd
            if name == "execute_command_sandboxed_v1":
                name = "execute_command"
                sandbox_policy = payload.get("sandbox")
                if sandbox_policy not in {"auto", "bwrap", "firejail"}:
                    raise ValueError("mandatory sandbox policy missing from bridged execute_command")
            else:
                sandbox_policy = None
            # #1317: honour the server-shipped active-project exec cwd so a bridged tool resolves relative
            # paths + runs execute_command THERE, not in the client's boot workdir (fail-soft: run_tool
            # ignores a cwd that doesn't exist on this host — remote/sealed → byte-identical fallback).
            result = gx10.run_tool(name, args, exec_cwd=payload.get("exec_cwd"),
                                   sandbox_policy=sandbox_policy)
        except Exception as e:  # noqa: BLE001 — never break the stream on a tool error
            result = f"ERROR: {e!r}"
        deadline = time.monotonic() + _TOOL_RESULT_POST_DEADLINE_S
        backoff = _TOOL_RESULT_POST_BACKOFF_S
        while True:
            try:
                self._req("POST", "/tool-result", {"id": rid, "result": result})
                return
            except urllib.error.HTTPError as e:
                if e.code == 410:
                    return  # stale bridge — the server moved on; stop resending
                if e.code in (401, 403):
                    # The session expired mid-turn. Reopen it so the completed result can still reach
                    # the bridge before its timeout; a failed reopen is retried with the result post.
                    try:
                        self.session_open()
                    except Exception:  # noqa: BLE001 — a failed reopen just means we retry again
                        pass
                elif 400 <= e.code < 500:
                    return  # other client errors are not recoverable — drop
                # 5xx is transient: fall through to the capped retry.
            except OSError:
                # transient transport failure — retry below. OSError (not just URLError) so a read-phase
                # socket.timeout / ConnectionResetError on resp.read() (NOT a URLError subclass) can't escape
                # and break the stream (the very failure this retry exists to absorb). HTTPError is caught
                # above, so this never swallows an HTTP status.
                pass
            if time.monotonic() >= deadline:
                return
            time.sleep(backoff)
            backoff = min(backoff * 2, _TOOL_RESULT_POST_MAX_BACKOFF_S)

    def cancel(self) -> Dict[str, Any]:
        """Abort the turn currently running on the server (sets its cancel event)."""
        return self._req("POST", "/cancel", {})

    def tasks(self) -> List[Dict[str, Any]]:
        return self._req("GET", "/tasks").get("tasks", [])

    def pending(self) -> List[Dict[str, Any]]:
        return self._req("GET", "/pending").get("pending", [])

    def claim(self, task_id: str, agent: str) -> Dict[str, Any]:
        return self._req("POST", "/claim", {"task_id": task_id, "agent": agent})

    def unclaim(self, task_id: str) -> Dict[str, Any]:
        return self._req("POST", "/unclaim", {"task_id": task_id})

    def coders(self) -> Dict[str, Any]:
        return self._req("GET", "/coders")

    def set_coder_pin(self, agent: str) -> Dict[str, Any]:
        return self._req("POST", "/coders", {"agent": agent})

    def feedback(self, task_id: str, agent: str, content: str,
                 exit_code: Optional[int] = None, stderr: str = "") -> Dict[str, Any]:
        # #455: also report the raw run signal (exit code + a stderr tail) so the server can classify
        # a budget-exhausted run and fail over. Back-compatible: omitted ⇒ today's feedback-only post.
        return self._req("POST", "/feedback",
                         {"task_id": task_id, "agent": agent, "content": content,
                          "exit_code": exit_code, "stderr": stderr})


# --------------------------------------------------------------------------- #
# Client-managed transport (Phase d, sealed profile) — generic, SSH-agnostic.
# --------------------------------------------------------------------------- #
class Tunnel:
    """Runs a configured forward command (e.g. an SSH local-forward) as a child so the
    server becomes reachable at ``base_url``, and tears it down on exit. Generic: the
    command (and thus the SSH/host specifics) is supplied by the operator's private
    config via ``GX10_TUNNEL_CMD`` — never hard-coded here. When the CLI exits, the
    child dies and the forward closes → the channel seals, OS-enforced."""

    def __init__(self, cmd: str, base_url: str, log=print) -> None:
        self.cmd = cmd
        self.base_url = base_url
        self.log = log
        self.proc: Optional[subprocess.Popen] = None

    def _addr(self) -> tuple[str, int]:
        u = urllib.parse.urlparse(self.base_url)
        return (u.hostname or "localhost", u.port or 8100)

    def _close(self) -> None:
        """Tear the forward child down (idempotent)."""
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def __enter__(self) -> "Tunnel":
        host, port = self._addr()
        self.log(f"  → opening transport: {self.cmd.split()[0]} … (forward → {host}:{port})")
        self.proc = subprocess.Popen(shlex.split(self.cmd), stdin=subprocess.DEVNULL)
        # Wait for the local end of the forward to accept connections. ANY failure here
        # (early exit, timeout, bad host) must tear the child down — __exit__ is NOT
        # called when __enter__ raises, so an orphaned ssh would otherwise linger.
        try:
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline:
                if self.proc.poll() is not None:
                    raise RuntimeError(f"tunnel command exited early (rc={self.proc.returncode})")
                try:
                    with socket.create_connection((host, port), timeout=1.0):
                        self.log("  ✓ transport up")
                        return self
                except OSError:
                    time.sleep(0.4)
            raise RuntimeError(f"tunnel did not come up within 15s ({host}:{port})")
        except BaseException:
            self._close()
            raise

    def __exit__(self, *exc: Any) -> None:
        self._close()
        self.log("  ✓ transport closed (channel sealed)")


class _NullCtx:
    """No-op stand-in when no tunnel command is configured."""
    def __enter__(self):  # noqa: D401
        return self
    def __exit__(self, *exc):  # noqa: D401
        return None


def _heartbeat_loop(srv: "Server", interval: float, stop: threading.Event) -> None:
    """Keep the server-side session live while the CLI is open."""
    while not stop.wait(interval):
        if not srv.session_heartbeat():
            # Session lost server-side (restart / expiry) → try to re-open quietly.
            try:
                srv.session_open()
            except urllib.error.URLError:
                pass


# --------------------------------------------------------------------------- #
# Local code-agent execution (the code-locality half).
# --------------------------------------------------------------------------- #
def _stdin_ready(timeout: float) -> bool:
    """True if more input is already buffered (the trailing lines of a paste)."""
    try:
        if os.name == "nt":
            import msvcrt
            if timeout:
                time.sleep(timeout)
            return msvcrt.kbhit()
        import select
        return bool(select.select([sys.stdin], [], [], timeout)[0])
    except Exception:  # noqa: BLE001 — never break the REPL over input probing
        return False


def _read_input(prompt: str) -> str:
    """Read a line; if a multi-line paste arrived (several lines at once), gather it ALL
    into one turn. Uses the native terminal — scrollback and paste just work; this only
    keeps a pasted block from being split into many turns."""
    sys.stdout.write(prompt)
    sys.stdout.flush()
    first = sys.stdin.readline()
    if not first:
        raise EOFError
    lines = [first.rstrip("\r\n")]
    if _stdin_ready(0.05):                 # looks like a paste — let it fully land, then drain
        while _stdin_ready(0.0):
            nxt = sys.stdin.readline()
            if not nxt:
                break
            lines.append(nxt.rstrip("\r\n"))
    return "\n".join(lines)


def _read_capped(path: Path, cap: int = _FEEDBACK_MAX_BYTES) -> str:
    """Read at most *cap* bytes from a coder-controlled result file (a cap+1 probe bounds the allocation,
    so a multi-GB feedback file cannot OOM the client). An oversized file is truncated — the pipeline-
    driving `status:` line is the FIRST line, so a tail truncation is safe. Decodes UTF-8 fail-soft."""
    with open(path, "rb") as fh:
        raw = fh.read(cap + 1)
    return raw[:cap].decode("utf-8", "replace")


def _run_handover(item: Dict[str, Any], codedir: Path, log=print) -> Tuple[Optional[str], Dict[str, Any]]:
    """Run a single staged handover LOCALLY with ``claude --print`` and return the
    feedback text it wrote (or None if it produced none).

    The handover content is materialised into the hidden local ``.ironclad/agent/handovers/`` so
    claude reads it exactly as in a normal session; claude is expected to write
    ``.ironclad/agent/feedback/{id}_{AGENT}-feedback.md`` locally, which we read back.
    ``log`` is the output sink (default ``print``; the TUI passes ``gx10._ui_print``
    so messages land in the full-screen pane, not over the layout)."""
    tid = item.get("id") or ""
    agent = (item.get("agent") or "OPUS").upper()
    ho_name = item.get("handover_file") or f"{tid}_{agent}.md"
    ho_text = item.get("handover") or ""

    # Local agent scratch is kept OUT of the product tree: a hidden .ironclad/agent/ drop zone under the
    # client codedir (the handover round-trip is HTTP-mediated, independent of the server's initiative).
    ho_dir = codedir / ".ironclad" / "agent" / "handovers"
    ho_dir.mkdir(parents=True, exist_ok=True)
    ho_path = ho_dir / ho_name
    ho_path.write_text(ho_text, encoding="utf-8")

    # #449 (C0R-9): the SERVER resolves the agent's full spec from the config-driven registry and
    # ships it in the item — the client is a THIN RENDERER (no client-side registry, no agent→model
    # table). Precedence per field: an EXPLICIT client-side override (GX10_AGENT_CMD / GX10_CLAUDE_BIN —
    # the documented single-agent BYO path, review B round 4) > the server spec > the Claude default.
    # So a default server's OPUS/SONNET template never makes a deliberate client override unreachable.
    model = item.get("model") or "claude-opus-4-8"
    effort = item.get("effort") or DEFAULT_EFFORT
    bin_ = CLAUDE_BIN_OVERRIDE or item.get("bin") or CLAUDE_BIN
    template = AGENT_CMD_OVERRIDE or item.get("cmd_template") or DEFAULT_AGENT_CMD
    permission = item.get("permission") or CLAUDE_PERMISSION_MODE
    bypass_requested = (permission == "bypassPermissions"
                        or "--dangerously-skip-permissions" in template)
    if bypass_requested and item.get("permission_bypass") is not True:
        refused = "permission bypass requires the agent's capabilities.permission_bypass=true opt-in"
        log(f"  ✗ {refused} — handover {tid} skipped")
        return None, {"exit_code": None, "stderr_tail": refused}
    # #1307: BUILD PRODUCT CODE in the active project's code root — the server ships it per /pending item
    # (`cwd` = the engine's exec cwd for the active project = <project-root>/<code_subdir>). Honour it ONLY
    # when it is a real directory on THIS host — a remote/sealed client does not share the server's
    # filesystem, so that absolute path won't exist; fall back to the client codedir (today's behaviour,
    # byte-identical for that topology), as we also do when no cwd is shipped (older engine). Closes the
    # isolation escape where a coder launched after an in-session /switch spawned in the stale codedir.
    shipped_cwd = item.get("cwd")
    launch_cwd = shipped_cwd if (shipped_cwd and Path(shipped_cwd).is_dir()) else str(codedir)
    # #443 (review F-1): the scratch (handover in / feedback out) stays under codedir; the paths handed to
    # the coder are ABSOLUTE so the feedback round-trip is independent of the coder's cwd (the product
    # tree). The result paths are deterministic per (tid, agent) and the scratch persists across re-runs —
    # unlink BOTH before launching so a stale file from a prior failed attempt is never read as THIS run's.
    fb_dir = codedir / ".ironclad" / "agent" / "feedback"
    fb_dir.mkdir(parents=True, exist_ok=True)
    fb_path = fb_dir / f"{tid}_{agent}-feedback.md"
    cap_path = fb_dir / f"{tid}_{agent}-output.md"
    fb_path.unlink(missing_ok=True)
    cap_path.unlink(missing_ok=True)
    # CLI-agnostic prompt: the feedback-file convention is stated HERE (not via a
    # Claude-only .claude/CLAUDE.md), so any headless code-agent can fulfil the contract.
    prompt = (f"Autonomously read and complete the handover at {ho_path}. "
              f"Follow any agent guide in this repo (e.g. AGENTS.md / CLAUDE.md). When done, write a "
              f"short result summary to {fb_path}. The FIRST line of that file must be `status: done` when "
              f"complete (the pipeline advances ONLY on `status: done`), otherwise `status: blocked` or "
              f"`status: clarification_needed`.")
    # #480/#994-S10: the server resolves the read-only Memory MCP whenever a memory service is
    # configured and the agent ships an mcp_template. Empty when memory is unconfigured or the agent
    # has no mcp_template; the client only renders what the server sent.
    # #1307: the {feedback} capture path is ABSOLUTE so it is independent of the coder's cwd.
    argv = build_agent_argv(template, bin=bin_, model=str(model),
                            effort=str(effort), permission=permission,
                            prompt=prompt, feedback=str(cap_path), mcp=str(item.get("mcp") or ""))
    from ack.tooling_envelope import assert_authorized
    if "tooling_envelope" in item:
        verdict = assert_authorized(bin_, template, item["tooling_envelope"])
        refused = None if verdict else (verdict.reason or "tooling envelope refused malformed coder command")
    else:
        from tooling_envelope_runtime import _envelope_authorize
        refused = _envelope_authorize(bin_, template)
    if refused:
        log(f"  ✗ {refused} — handover {tid} skipped")
        return None, {"exit_code": None, "stderr_tail": refused}
    log(f"  → code-agent (local): {tid} ({agent}, {model}, effort={effort})  cwd={launch_cwd}")
    # #480: the spawned MCP (a sub-subprocess of the agent CLI) inherits the memory connection from the
    # agent's env — the connection travels here, NEVER on the MCP JSON-RPC wire (secret-free).
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    mcp_env = item.get("mcp_env")
    if isinstance(mcp_env, dict):
        env.update({str(k): str(v) for k, v in mcp_env.items()})
    timeout_s = item.get("timeout_s")
    try:
        timeout_s = float(timeout_s) if timeout_s is not None else _CODER_TIMEOUT_DEFAULT
    except (TypeError, ValueError):
        timeout_s = _CODER_TIMEOUT_DEFAULT
    # Defense-in-depth: a malformed /pending payload (<=0 fires an immediate kill; inf/NaN break the wait)
    # must not be trusted blindly — an out-of-sane-range value falls back to the default. 86400s (24h) is a
    # generous sanity ceiling well above the schema's 7200s max, so no valid value is ever rejected.
    if not (0 < timeout_s <= 86400):
        timeout_s = _CODER_TIMEOUT_DEFAULT
    try:
        # #455: capture stderr (and still surface it) so the server can classify a budget/quota
        # exhausted run as `agent-unavailable` and fail over instead of retrying forever.
        popen_args = dict(cwd=str(launch_cwd), env=env, stdin=subprocess.DEVNULL,
                          stderr=subprocess.PIPE)
        if os.name == "nt":
            popen_args["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_args["start_new_session"] = True
        proc = subprocess.Popen(argv, **popen_args)
    except FileNotFoundError:
        log(f"  ✗ code-agent binary '{argv[0] if argv else CLAUDE_BIN}' not found "
            f"(set GX10_CLAUDE_BIN / GX10_AGENT_CMD) — handover {tid} skipped")
        return None, {"exit_code": None, "stderr_tail": "binary-not-found"}
    tail = _BoundedTail()
    reader = threading.Thread(target=_drain_stderr, args=(proc.stderr, tail), daemon=True)
    reader.start()
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        # A hung coder holds its pool claim forever (#1491). Kill the whole tree and return the run signal:
        # the `timeout …` stderr matches no exhausted pattern → classified `task-failed` (a normal failure,
        # NOT budget-exhausted), so the server releases the claim and retries to budget rather than tripping
        # the breaker / failing over. Any feedback the coder wrote in its last instant is intentionally
        # dropped — a killed coder's partial result is not trusted as complete.
        proc_tree.kill_process_tree(proc)
        reader.join(2.0)
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001 — the process may already have exited
                pass
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                pass  # The daemon reader and OS process cleanup remain fail-soft after the bounded reap.
        log(f"  ✗ code-agent {tid} timed out after {timeout_s:.0f}s — killed")
        return None, {"exit_code": None, "stderr_tail": f"timeout after {timeout_s:.0f}s"}
    reader.join(2.0)
    rc = proc.returncode
    stderr = tail.text()
    if stderr.strip():
        log(stderr.rstrip())                                  # keep the agent's stderr visible
    meta = {"exit_code": rc, "stderr_tail": stderr[-_STDERR_TAIL_CHARS:]}

    if fb_path.exists():
        return _read_capped(fb_path), meta
    # #443 hybrid fallback: the agent didn't write the feedback file — use its captured final message
    # (`-o {feedback}`, written THIS run since we unlinked stale copies above) if present, so a forgotten
    # feedback file no longer yields a silent no-feedback retry.
    if cap_path.exists():
        text = _read_capped(cap_path)
        if text.strip():
            log(f"  ⓘ no feedback file {fb_path.name}; using the captured final message {cap_path.name}")
            return text, meta
    log(f"  ⚠ agent exited (exit {rc}) without a feedback file {fb_path.name} or a captured message")
    return None, meta


def _renew_claim(srv: Server, tid: str, agent: str, stop: threading.Event) -> None:
    """Best-effort lease renewal while a local coder owns the task."""
    while not stop.wait(_CLAIM_RENEW_INTERVAL_S):
        try:
            srv.claim(tid, agent)
        except Exception:   # noqa: BLE001 — lease loss must never interrupt the coder
            pass


def _process_one(srv: Server, codedir: Path, item: Dict[str, Any], claimed: set,
                 log=print) -> bool:
    """One pool job: run the handover locally, upload its feedback. On any failure
    the task is UNclaimed so the next poll retries it. Returns True on a clean upload."""
    tid = item.get("id") or ""
    agent = (item.get("agent") or "OPUS").upper()
    renew_stop: Optional[threading.Event] = None
    renew_thr: Optional[threading.Thread] = None
    try:
        srv.claim(tid, agent)
    except Exception as e:  # noqa: BLE001 — an older/unreachable server must not block a working coder
        log(f"  ⚠ {tid}: /claim failed (continuing): {e}")
    else:
        renew_stop = threading.Event()
        renew_thr = threading.Thread(target=_renew_claim, args=(srv, tid, agent, renew_stop), daemon=True)
        renew_thr.start()
    try:
        fb, meta = _run_handover(item, codedir, log=log)
        # #455: ALWAYS report the run signal (even with no feedback) so the server can classify a
        # budget-exhausted run → trip the breaker + fail over on the next poll, instead of retrying
        # the same out-of-budget agent forever. The lease is renewed THROUGH the feedback upload so a
        # slow post can't let it lapse into a double-run (#1525).
        res = srv.feedback(tid, agent, fb or "",
                           exit_code=meta.get("exit_code"), stderr=meta.get("stderr_tail", ""))
        cls = res.get("classification")
        if cls == "ok-feedback" or (cls is None and fb):
            log(f"  ✓ feedback uploaded: {tid} → {res.get('feedback_file')}")
            return True
        if cls == "agent-unavailable":
            log(f"  ⚠ {tid}: {agent} unavailable (budget/quota) → failing over to a peer on the next poll")
        else:
            log(f"  ⚠ {tid}: no feedback produced — will retry on the next poll")
    except urllib.error.URLError as e:
        log(f"  ✗ {tid}: upload/network failed: {e}")
    except Exception as e:  # noqa: BLE001
        log(f"  ✗ {tid}: code-agent failed: {e!r}")
    finally:
        # #1525: stop AND join the lease-renewal thread BEFORE any /unclaim below, so an in-flight
        # renewal /claim can never land after the release and re-stamp the task in_progress (which would
        # wedge it until the lease TTL, blocking the immediate retry).
        if renew_stop is not None:
            renew_stop.set()
        if renew_thr is not None:
            renew_thr.join(timeout=5.0)
    try:
        srv.unclaim(tid)
    except Exception as e:  # noqa: BLE001 — reconciler/stall watchdog remain the backstop
        log(f"  ⚠ {tid}: /unclaim failed (continuing): {e}")
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
        # CLI-3 (#503): atomic check-then-claim under a lock — an overlapping /auto poll + /work (or two
        # poll ticks) could otherwise both pass `tid in claimed` and double-launch the same handover.
        with _CLAIM_LOCK:
            if not tid or tid in claimed:
                continue
            claimed.add(tid)  # claim immediately → no double-launch on the next poll
        futures.append(pool.submit(_process_one, srv, codedir, item, claimed, log))
    return futures


# --------------------------------------------------------------------------- #
# REPL.
# --------------------------------------------------------------------------- #
def _style_stream_line(line: str) -> str:
    """Lightly colour one streamed output line so the technical markers recede and the
    completion line stands out — without touching the answer body."""
    s = line.lstrip()
    if "[perf]" in s or "[agent]" in s:                  # #453: routing provenance recedes like [perf]
        return _c(line, "gray")
    if "===" in s and "DONE" in s:
        return _c(line, "green")
    if s.startswith("[") and ("Qwen" in s or "GX10" in s or "planning" in s):
        return _c(line, "dim")
    if s.startswith("✗") or s.startswith("⚠"):
        return _c(line, "yellow")
    return line


def _print_tasks(tasks: List[Dict[str, Any]]) -> None:
    if not tasks:
        print("  (no tasks)")
        return
    for t in tasks:
        print(f"  {t.get('status','?'):11} {t.get('id','?'):10} "
              f"{t.get('type','?'):14} {t.get('title','')}")


def http_error_msg(e: "urllib.error.HTTPError") -> str:
    """Best-effort: the server's JSON ``{"error": …}`` from an HTTPError body (e.g. the friendly
    'unknown agent …' from POST /coders), else the raw error. Shared by the clients (#454)."""
    try:
        return json.loads(e.read().decode()).get("error", str(e))
    except Exception:
        return str(e)


def render_guide(g: Dict[str, Any], emit) -> None:
    """#955: render a server ``needs_guide`` contract (#954) as plain lines via ``emit(str)`` — the fields
    the operator must supply, from the command-spec. Client chrome is English (thin renderer); shared by
    the three Python clients so the rendering never drifts between them."""
    emit(f"  guided input for /{g.get('command', '?')}:")
    emit(f"    usage: {g.get('usage', '')}")
    if g.get("subcommands"):
        emit("    subcommands: " + " | ".join(g["subcommands"]))
    for f in g.get("fields", []):
        bits = ["required" if f.get("required") else "optional"]
        if f.get("choices"):
            bits.append("choices: " + "|".join(f["choices"]))
        if f.get("default"):
            bits.append("default: " + str(f["default"]))
        emit(f"    {f.get('name', '')}  ({', '.join(bits)})")


def _print_coders(data: Dict[str, Any]) -> None:
    """#452: render which coding agents are bound (● green) vs not found (○ red), then the fan-out
    provider lane (active/spend + per-provider reachability + last routing reason)."""
    coding = data.get("coding_agents") or []
    pinned = data.get("pinned")
    if pinned:
        print(_c(f"  pinned: {pinned}", "cyan") + _c("  (/coders use auto to clear)", "gray"))
    else:
        print(_c("  routing: auto (orchestrator's staged agent per task)", "gray"))
    if not coding:
        print("  (no coding agents configured)")
    for a in coding:
        enabled = a.get("enabled", True)               # #460: False ⇒ onboarded but not yet activated
        bound = a.get("bound")
        dot = _c("◌", "gray") if not enabled else (_c("●", "green") if bound else _c("○", "red"))
        is_pin = pinned and str(a.get("id", "")).upper() == str(pinned).upper()
        if not enabled:
            suffix = _c("  (onboarded · disabled)", "gray")
        elif is_pin:
            suffix = _c("  ← pinned", "cyan")
        else:
            suffix = "" if bound else _c("  (binary not found)", "gray")
        print(f"  {dot} {str(a.get('id','?')):8} {a.get('model','—')}" + suffix)
    prov = data.get("providers") or {}
    pool = prov.get("pool") or []
    if pool:
        b = prov.get("budget") or {}
        cap = b.get("usd_cap")
        head = (f"  providers (fan-out): {'active' if prov.get('active') else 'inactive'}"
                f"  ·  spent ${b.get('spent_usd', 0):.4f}")
        if cap is not None:
            head += f" / ${cap}"
        print(_c(head, "gray"))
        for p in pool:
            dot = _c("●", "green") if p.get("reachable") else _c("○", "red")
            tail = f"  ← {p.get('last_route_reason')}" if p.get("last_route_reason") else ""
            print(f"    {dot} {str(p.get('id','?')):14} {str(p.get('kind','?')):9} {p.get('model','—')}{tail}")


def repl(srv: Server, codedir: Path, max_agents: int = DEFAULT_MAX_AGENTS) -> None:
    try:
        h = srv.health()
        _print_banner(srv, h, codedir, max_agents, reachable=True)
    except urllib.error.URLError as e:
        _print_banner(srv, {}, codedir, max_agents, reachable=False)
        print(_c(f"  ⚠ server {srv.base} unreachable: {e}", "yellow"))

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
            line = _read_input("\n" + _c("❯", "bcyan", "bold") + " ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        kind, name, payload = classify(line)
        if kind == "empty":
            continue
        if kind == "suggest":   # #934: unknown command → did-you-mean hint, never forwarded (no turn)
            print(f"  unknown command — did you mean  /{name} ?")
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
            elif name == "doctor":   # DOCTOR (#503): local — GET /doctor, don't forward (no billed turn)
                try:
                    print("  " + json.dumps(srv.doctor(), ensure_ascii=False))
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
            elif name == "coders":
                try:
                    parts = payload.split()
                    if len(parts) >= 2 and parts[1].lower() == "use":  # /coders use <id>|auto
                        arg = parts[2] if len(parts) >= 3 else "auto"
                        res = srv.set_coder_pin(arg)
                        pin = res.get("pinned")
                        print(_c(f"  → pinned coder: {pin}" if pin
                                 else "  → coder pin cleared (auto: the staged agent per task)", "cyan"))
                    _print_coders(srv.coders())
                except urllib.error.HTTPError as e:
                    print(f"  ✗ {http_error_msg(e)}")
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
                    print(f"  [AUTO] {'ON' if auto_stop else 'OFF'}  |  /auto on / /auto off")
            continue
        # kind in ("server", "turn") → to the orchestrator (a server command without / or a turn).
        # Stream so code-tools are passed through to us and run on the LOCAL filesystem.
        try:
            buf = {"s": ""}

            def _emit(t: str) -> None:
                # Line-buffer so each complete line can be styled; the terminal keeps
                # native scroll/copy because we just print into its scrollback.
                buf["s"] += t
                while "\n" in buf["s"]:
                    out, buf["s"] = buf["s"].split("\n", 1)
                    print(_style_stream_line(out), flush=True)
            res = srv.chat_stream(payload, _emit)
            if res and res.get("needs_confirm"):   # #935: destructive → not executed; re-run with --yes
                ci = res["needs_confirm"]
                # #956: the reason is the full localized line (reason + how-to-confirm) → print it single-language
                print(_c(f"  ⚠ {ci.get('command', '?')}: {ci.get('reason', 'destructive command')}", "yellow"))
            elif res and res.get("needs_guide"):   # #955: structured guided input — show fields, don't execute
                render_guide(res["needs_guide"], lambda s: print(_c(s, "yellow")))
            if buf["s"]:
                print(_style_stream_line(buf["s"]), flush=True)
        except urllib.error.URLError as e:
            print(_c(f"  ✗ /chat failed: {e}", "red"))

    if auto_stop is not None:
        auto_stop.set()
    pool.shutdown(wait=False, cancel_futures=True)


def _establish_session(srv: "Server") -> tuple[Optional[threading.Event], Optional[threading.Thread]]:
    """If the server's profile requires a session (sealed), open one and keep it alive
    with a heartbeat thread. Returns (stop_event, thread) or (None, None) if no session
    is needed. The deployment secret (if any) is already on ``srv``."""
    try:
        h = srv.health()
    except urllib.error.URLError as e:
        print(f"  ⚠ server unreachable for handshake: {e}")
        return None, None
    sec = h.get("security") or {}
    if not sec.get("session"):
        return None, None
    hb = float(sec.get("heartbeat_s") or 30)
    try:
        res = srv.session_open()
    except urllib.error.HTTPError as e:
        hint = (" — set GX10_SERVER_TOKEN to the server's deployment secret"
                if e.code == 401 else "")
        print(f"  ✗ could not open a session (HTTP {e.code}){hint}")
        return None, None
    except urllib.error.URLError as e:
        print(f"  ✗ could not open a session: {e}")
        return None, None
    print(f"  ✓ session opened ({(res.get('session_id') or '?')[:8]}…, heartbeat {hb:.0f}s)")
    stop = threading.Event()
    t = threading.Thread(target=_heartbeat_loop, args=(srv, hb, stop), daemon=True)
    t.start()
    return stop, t


def main() -> None:
    p = argparse.ArgumentParser(description="Ironclad thin orchestrator client")
    p.add_argument("--server", default=DEFAULT_SERVER,
                   help=f"Orchestrator server URL (default {DEFAULT_SERVER})")
    p.add_argument("--codedir", default=".",
                   help="Local code root (cwd for claude --print; contains .claude/CLAUDE.md)")
    p.add_argument("--max-agents", type=int, default=DEFAULT_MAX_AGENTS,
                   help=f"Max parallel local code-agents (default {DEFAULT_MAX_AGENTS})")
    args = p.parse_args()
    setup_output()   # UTF-8-safe stdout + ANSI on Windows
    srv = Server(args.server, token=SERVER_TOKEN)
    codedir = Path(args.codedir).expanduser().resolve()
    os.chdir(codedir)   # passed-through code-tools (run_tool) act on YOUR local code root

    # Phase d: open the transport first (sealed profile), then the session handshake.
    transport = Tunnel(TUNNEL_CMD, args.server) if TUNNEL_CMD else _NullCtx()
    with transport:
        stop_hb, _ = _establish_session(srv)
        try:
            repl(srv, codedir, max_agents=args.max_agents)
        finally:
            if stop_hb is not None:
                stop_hb.set()
            srv.session_close()


if __name__ == "__main__":
    main()
