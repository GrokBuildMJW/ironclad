# engine/ — Orchestration engine

The orchestration engine: agent loop, deterministic TaskStore, fail-closed macros
(`stage_handover` / `advance_pipeline`), config-tree loader, platform mode. Every
model-emitted `task_json` is validated against the ACK contract at the
`stage_handover` boundary (mandatory base validation; the remaining stricter schema selector is
`lodestar.enabled`). The typed configuration schema generates the complete public
[`config-runtime.md`](../docs/config-runtime.md) reference, and CI proves its live switch inventory matches
the engine's operational reads. Runs against any OpenAI-compatible endpoint.

## Modules

> **The recommended interactive client is now the TypeScript terminal client in
> [`clients/ink/`](../clients/ink/)** (purpose-built renderer: ghost-free resize, smooth
> streaming, native scrollback/selection/copy). The Python clients below — `client.py`,
> `tui.py`, `cli.py` — are **legacy**: kept as zero-dependency references and headless
> fallbacks, still maintained but no longer the primary UI.

- `gx10.py` — the orchestration **engine library** (agent loop, mandatory untrusted-result fencing,
  sandbox-required model tool execution, deterministic TaskStore, fail-closed macros, config-tree loader,
  context trimming). Imported by the server;
  the standalone monolithic CLI was **removed** (one way: server + client).
- `server.py` — **headless** orchestrator server. Drives `gx10` with no UI and
  exposes a plain-HTTP API. Holds the reasoning + state (turn loop, TaskStore,
  `stage_handover`/`advance_pipeline`, feedback-side reconciler). Run on the box that
  sits next to the model.
- `client.py` — **thin client (legacy)**. Connects like the CLI connects to the model
  (plain LAN HTTP, client-initiated). Holds the conversation REPL and *code locality*:
  project code stays on this machine and the code-agents (`claude --print`) run here,
  in a bounded pool (`--max-agents`) so independent handovers run in parallel.
- `tui.py` — **full-screen client (legacy)**. The old GX10 look-and-feel (prompt_toolkit
  output pane + branded bottom toolbar) over the split: turns stream live via
  `/chat/stream`, the toolbar shows remote status (model, perf, task counts,
  watcher/autopilot, connection). Reuses `gx10`'s UI primitives; falls back to the
  line REPL if prompt_toolkit is absent.
- `cli.py` — **Rich-based client (legacy)**. The framework-free Claude-Code-style
  predecessor whose look-and-feel the `clients/ink/` client reimplements.
- `workers.py` — **reasoning workers**. Server-side fan-out of independent
  reasoning/planning prompts to concurrent local-model requests (co-located with the
  GPU; no code access). Exposed as `POST /fanout`. Stateless — does not take the
  agent lock.

## Server/client split

```
PC (client.py)                         Spark (server.py + vLLM)
  REPL ── POST /chat ───────────────▶  GX10 turn loop, TaskStore, stage_handover
  pull ── GET  /pending ────────────▶  staged handovers (pending + handover file)
  claude --print  (LOCAL code) 
  upload ─ POST /feedback ──────────▶  reconciler advances the task
```

The server never reaches into the client; the client initiates every exchange, so
session-gating and code-locality are structural. **The trust model is selectable (Phase d):**
`open` (no auth; **binds loopback only by default** since #1469 — a non-loopback `open` bind refuses
fail-closed at boot unless you set the explicit `GX10_ALLOW_UNAUTHENTICATED_BIND=1` override), `token`
(deployment secret over the LAN), or `sealed` (secret + session heartbeat, typically over a
client-managed tunnel; may bind the LAN under the secret).
The token is a deployment secret, not a user login. Pass the server address via `--server` /
`GX10_SERVER_URL`, never hard-code it. The local code-agent is pluggable via `GX10_AGENT_CMD`
(not locked to `claude --print`).

Model `execute_command` is stricter than ordinary code locality: the execution host must provide `bwrap` or
`firejail`, otherwise the tool refuses before a subprocess starts. The server uses a versioned bridge frame
for client-local model commands so an older client cannot run the former direct-shell path. Windows refuses
model commands; Ink `/sh` is a separate operator channel. Every untrusted result from either native or
bridged execution is fenced once before it enters model context.

```bash
# on the model box (a LAN bind under `open` needs the explicit override; prefer token/sealed for real use):
GX10_ALLOW_UNAUTHENTICATED_BIND=1 python engine/server.py --host 0.0.0.0 --port 8100
# on the PC:
GX10_SERVER_URL=http://<server>:8100 python engine/client.py --codedir .
```
