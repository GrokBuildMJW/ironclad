# core/engine/ — Orchestration engine

The orchestration engine: agent loop, deterministic TaskStore, fail-closed macros
(`stage_handover` / `advance_pipeline`), config-tree loader, platform mode. Every
model-emitted `task_json` is validated against the ACK contract at the
`stage_handover` boundary (soft path; config-driven via `ack.enabled` /
`lodestar.enabled`). Runs against any OpenAI-compatible endpoint.

## Modules

- `gx10.py` — the engine + the interactive CLI (`python gx10.py`).
- `server.py` — **headless** orchestrator server. Drives `gx10` with no UI and
  exposes a plain-HTTP API. Holds the reasoning + state (turn loop, TaskStore,
  `stage_handover`/`advance_pipeline`, feedback-side reconciler). Run on the box that
  sits next to the model.
- `client.py` — **thin client**. Connects like the CLI connects to the model (plain
  LAN HTTP, client-initiated). Holds the conversation REPL and *code locality*:
  project code stays on this machine and the code-agents (`claude --print`) run here,
  in a bounded pool (`--max-agents`) so independent handovers run in parallel.
- `tui.py` — **full-screen client**. The old GX10 look-and-feel (prompt_toolkit
  output pane + branded bottom toolbar) over the split: turns stream live via
  `/chat/stream`, the toolbar shows remote status (model, perf, task counts,
  watcher/autopilot, connection). Reuses `gx10`'s UI primitives; falls back to the
  line REPL if prompt_toolkit is absent.
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
session-gating and code-locality are structural. Trust model = home LAN (no auth on
the port, same as the vLLM port). The real server address is private — pass it via
`--server` / `GX10_SERVER_URL` (from `conf/`), never hard-coded in `core/`.

```bash
# on the model box:
python core/engine/server.py --host 0.0.0.0 --port 8100
# on the PC:
GX10_SERVER_URL=http://<server>:8100 python core/engine/client.py --codedir .
```
