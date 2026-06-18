# Project status & wiring

> ## ⚠️ Read this first
>
> Ironclad's engine grew out of a **proven, in-production orchestrator** (years of
> daily use). It is currently undergoing a **complete redesign**: the original
> single-process CLI is being split into a headless **server** + thin **client**,
> containerized, and given a reasoning-worker fan-out and a purpose-built TypeScript terminal client.
>
> **Not everything is re-wired or re-tested yet.** The pieces below are marked
> honestly: *proven* (inherited, battle-tested), *wired + tested* (rebuilt and
> verified in the new architecture), *placeholder* (hook exists, backend/logic not
> yet ported), or *opt-in* (off by default). Treat `main` as a development snapshot.

This document is the single source of truth for **what actually works right now**.

## Module reference

| File | What it does |
|------|--------------|
| `engine/gx10.py` | The orchestrator **engine library** (the *proven* core). Agent loop, tool execution, deterministic `TaskStore`, fail-closed macros (`stage_handover` / `advance_pipeline`), config-tree loader, platform detection, context trimming. The server imports it; the standalone monolithic CLI was **removed** (one way: server + client). |
| `engine/server.py` | **Headless server** (new). Drives the engine with no UI, exposes plain-HTTP endpoints, serializes agent access, runs the feedback-side reconciler. |
| `clients/ink/` | **Recommended terminal client** (new, TypeScript). Purpose-built renderer (own React reconciler + flexbox + packed cell buffer + cell-level diff) → **ghost-free resize**, smooth streaming, native scrollback/selection/copy. Slash-command autocomplete, local `!cmd` shell, in-CLI `/update`, per-project session. Same HTTP/tool-bridge contract; build-from-source (Node ≥ 22). |
| `engine/client.py` | **Thin client (legacy)**. Line REPL + the local code-agent pool (`claude --print`) that keeps project code on your machine. Zero-dependency fallback. |
| `engine/tui.py` | **Full-screen client (legacy)**. prompt_toolkit UI, live streaming, status toolbar, scrollback, compressed multi-line paste. |
| `engine/commands.py` | Shared command router for REPL + TUI (`/command` → local or forwarded to the server). |
| `engine/workers.py` | **Reasoning-worker fan-out** (new). Independent prompts run concurrently against the model (`POST /fanout`). |
| `ack/` | **Agent-Contract-Kernel**: schema-as-SSOT (`case_spec`), bounded validate→reask (`validated_emit`), constrained emission (`constrained_emission`), registry, doctor, generator, and the opt-in `lodestar` plugin. |

## Wiring status

| Component | Status | Notes |
|-----------|--------|-------|
| Agent loop, tools, TaskStore, macros | **proven** | Inherited from the in-production orchestrator. |
| Config tree + `GX10_*` env + language setting | **wired + tested** | `language` (reply language) default `en`. |
| Server/client split (`/chat`, `/chat/stream`, `/tasks`, `/pending`, `/feedback`) | **wired + tested** | Verified PC→LAN→Spark; headless capture; streaming. |
| Thin client + parallel code-agent pool | **wired + tested** | Bounded pool; claim-once; unclaim-on-failure (in both the TypeScript and Python clients). |
| **TypeScript terminal client** (`clients/ink/`) | **wired + tested** | Purpose-built renderer (React reconciler + Yoga flexbox + packed cell buffer + cell-level diff). **327** `node:test` cases across renderer/UI/net/tools; ghost-free resize, native scrollback/selection/copy, live-streaming markdown with preserved + syntax-highlighted code, slash-command autocomplete, `!cmd`, in-CLI `/update`, per-project session; adversarially verified. Build-from-source / global install (Node ≥ 22). |
| Full-screen Python TUI (stream, toolbar, scroll, paste) — **legacy** | **wired + tested** | Render/scroll/paste unit-checked; runs in a real terminal. Superseded by the TypeScript client. |
| Reasoning-worker fan-out (`/fanout`) + `parallel_reason` tool | **wired + tested** | Stateless concurrent reasoning, **GPU-safety-governed**: a config-driven concurrency cap **and** a token-budget envelope (`concurrency × max_tokens ≤ max_batch_tokens`) so a large `max_tokens` lowers parallelism instead of over-subscribing the box — overflow just queues. Core ships conservative defaults (concurrency 4); the deploy pins model-matched values (8 = our model's `max_num_seqs`). Now also exposed **in-engine** as the `parallel_reason` tool, so the orchestrator itself batches independent items. Live-verified: governed `/fanout` 8/8 at ~5.8× speedup on the reference GPU. |
| ACK contract gate at `stage_handover` | **wired + tested** | Soft path (`_ack_validate`): task_json validated against `TaskSpec`, fail-closed → reask. |
| Function-calling robustness (every tool boundary) | **wired + tested** | Validate→reask on **all** tool arguments, not just task_json: malformed JSON or a schema violation (required + types) is returned to the model as the tool result so it re-emits — never silently degraded to empty args. Live-verified end to end (native tool turn against the reference model). |
| Model-agnostic tool-call recovery | **wired + tested** | When an OpenAI-compatible endpoint returns no native `tool_calls`, calls the model emitted as **text** (`<tool_call>` tags, fenced json, or a bare object) are recovered — gated to known tool names so a legitimate JSON answer is never hijacked. Removes the hard dependency on a server-side tool parser. |
| Orchestrator Docker image / compose | **wired + tested** | Runs as `ironclad-orchestrator` next to the model. |
| **Memory — multi-tier** (Mem0 cold + warm tier + per-turn context pipeline) | **wired + tested** | Cold: `engine/memory.py` → Mem0-style service (`GX10_MEMORY_URL`). Warm: `engine/warm.py` → a BSD in-memory store (`GX10_WARM_URL`, fail-soft). Plus rolling summarization, per-turn RAG (with warm cache-aside), token-accurate budgeting, chunking + recency, the `deep_query_memory` (graph) tool, and worker memory (shared summary on read + single-writer reduce). Default-on where a memory/warm service is configured; store starts **empty** — see below. |
| **Autoplan** (`/autoplan on\|off [N]`) | **wired (config-gated)** | Ported into the server's queue consumer (`_autoplan_tick`), **decoupled from autopilot** so it works in the split: server plans → client executes → server advances → server plans again. Fires only when `/autoplan on` is set **and** `paths.active_capability_backlog` is configured (no backlog → it disables itself). Logic unit-tested. |
| Autopilot auto-launch on the server | **placeholder / by design off** | The server never launches code-agents (`_LAUNCH_CMD` is skipped); launching is the client's job (the pool). The server-side `autopilot` toggle is currently inert. |
| Remote turn cancel (Esc / Ctrl+C) | **wired + tested** | `POST /cancel` sets the engine cancel event; the running turn aborts at its next iteration. In the recommended TypeScript client **Esc or Ctrl+C** cancels — it aborts the stream locally (instant return to idle) and POSTs `/cancel`; the legacy Python TUI fires it via Ctrl+C non-blocking. |
| Constrained-emission **hard floor** (grammar) | **available — soft path active by design** | Grammar-constrained decoding (guided JSON) is **verified stable** on the reference GPU (no engine crash, schema-valid output). The ACK exposes it (`constrained_emission` / `emit_validated`) for callers wanting token-level guarantees; the **orchestration engine deliberately keeps the soft validate→reask gate** — it's backend-agnostic (any OpenAI endpoint) and already ~100% reliable, so per-emission grammar buys little. Not a TODO — a decision. |
| **Lodestar** capability→backlog plugin | **opt-in (off)** | `lodestar.enabled=false` by default; demo in `examples/demo-vessel/`. |
| **Security / trust model (Phase d)** | **wired + tested (single-tenant)** | Selectable trust profiles (`security.profile`): `open` (default — no auth, LAN bind, mount allowed), `token` (deployment secret over the LAN), `sealed` (loopback bind behind a client-managed tunnel + secret + session heartbeat; client-facing endpoints refuse and autoplan pauses when no session is live). The token is a **deployment secret, not a user login**. Fail-closed: an auth profile refuses to boot without a secret. Unit-tested end-to-end (no vLLM) **and live-verified** over a real SSH tunnel on the reference GPU (loopback bind, gated routes 401, session open→live→close, a real model turn through the sealed channel — see below). **No multi-user identity/authorization** — single principal only; see the [roadmap](roadmap.md). |
| **Open plugin surface** (`GX10_PLUGINS_DIR`) | **wired + tested** | The single open, versioned extension contract: drop a Python file with a module-level `CASE` dict + `run(...)` into a `skills/` directory, point `GX10_PLUGINS_DIR` at it, and the engine **discovers it and exposes it as an agent tool — with no core change**. The boundary check forbids the reverse (a plugin can never patch `core/`). See [`plugin-api.md`](plugin-api.md). |
| **Pluggable code-agent CLI** (`GX10_AGENT_CMD`) | **wired + tested** | The local code-agent the client launches per handover is a **command template**, not hard-wired to Claude Code — set `GX10_AGENT_CMD` to plug in any headless coding CLI with no code change (placeholders for prompt/codedir/permission-mode). See [`code-agents.md`](code-agents.md). |
| **Runtime contract self-check** (`GET /doctor`) | **wired + tested** | The same preflight the doctor CLI runs, exposed live so contract/registry drift surfaces at runtime (error/warn counts + a boot summary). Includes opt-in lodestar checks when enabled. |
| **Dev environment** (`Dockerfile.dev` + `docker-compose.dev.yml`) | **built (build+test gate)** | A reproducible, isolated dev setup that **builds the engine and runs the full suite inside a container** — the green build+test artifact is the precondition to promote. Resource-limited dev orchestrator, `open` profile, endpoint defaults to the reference stack and is overridable. Not needed to merely *use* Ironclad. See [`dev-environment.md`](dev-environment.md). |

## Memory

The engine has long-term memory **wired in**: a `query_memory` tool, store-on-task-
completion, and stage-time context injection, backed by `engine/memory.py` — a small,
secret-free client for a **Mem0-style HTTP service** (`POST /add`, `POST /search` with
`graph=false`, `GET /health`). Store + search are verified live against the reference
Mem0 stack (Qdrant + Neo4j + BGE-M3, LLM pointed at the local model).

**Three tiers (all server-side, fail-soft).** *Hot* = the model window (a bounded working
set, char/token-budget trimmed). *Warm* = `engine/warm.py`, a BSD-licensed in-memory store
(`GX10_WARM_URL`, e.g. Valkey) holding the rolling conversation summary, recent-turn state,
and a short-TTL retrieval cache that **survives a restart** and is **shared across the
reasoning workers**. *Cold* = the Mem0 vector(+graph) store below. On top of the tiers:
rolling/hierarchical summarization on eviction (raw archived to cold, prefix-stable),
per-turn RAG assembly (warm cache-aside), token-accurate budgeting that scales the working
set to the model window, chunked + recency-ranked long-artifact storage, an on-demand
`deep_query_memory` graph tool, and worker memory (workers read the shared summary +
per-item retrieval, a single-writer reducer consolidates their writes). Each piece is
individually flag-gated (default-on where a memory/warm service is configured); with the
warm/cold tiers down a turn still completes. The hot read path stays vector-only and off the
timeout-prone graph path.

**This repo ships the wiring, never any memory content.** Memory is a runtime service,
not data in the codebase:

- **Off by default.** With no `GX10_MEMORY_URL` (and no `conf/memory/memory.json`) the
  `MemoryManager` is never constructed → all hooks stay inert and no tool is offered.
- **Bring your own service _and_ corpus.** Point the engine at your Mem0 endpoint
  (`GX10_MEMORY_URL=http://your-mem-host:8800`, optional `GX10_MEMORY_AGENT`). The store
  starts **empty** — Ironclad accumulates its own memory from task completions; any
  pre-existing corpus is yours to import, into your own namespace, and never lives here.
- **Namespace.** Reads/writes use `agent_id` (default `ironclad`) and an optional
  `user_id`; set them to match wherever your content lives.

So the public artifact is **wired but empty** — the integration works, the content is
the operator's.

## Rebuild punch-list — done

1. ~~Wire the engine to the Mem0 backend.~~ **Done** — `engine/memory.py`, off by
   default, ships empty (see Memory above).
2. ~~Port the autoplan loop into the server's queue consumer.~~ **Done** —
   `_autoplan_tick`, decoupled from autopilot, backlog-config-gated.
3. ~~Wire remote turn cancellation across the HTTP boundary.~~ **Done** —
   `POST /cancel` + Esc/Ctrl+C in the recommended client (Ctrl+C in the legacy TUI).
4. ~~Decide the grammar hard-floor.~~ **Decided** — verified stable on the reference
   GPU; available via the ACK; the engine keeps the soft path by design (see table).

The rebuild's known placeholders are now resolved. What remains is ordinary
hardening and breadth of testing — treat `main` as a development snapshot still.

## Extension & self-maintenance — what's done vs in development

Ironclad is meant to be **extended and maintained through itself**, with a clean DEV/Prod
split. Honest split of that capability:

- **Done (wired + tested) — the user-facing surface.** The **open plugin API**
  (`GX10_PLUGINS_DIR`, [`plugin-api.md`](plugin-api.md)), the **pluggable code-agent CLI**
  (`GX10_AGENT_CMD`, [`code-agents.md`](code-agents.md)), the **dev container** build+test
  gate ([`dev-environment.md`](dev-environment.md)), the runtime `GET /doctor` self-check,
  and the beginner **self-maintenance guide** ([`self-maintenance.md`](self-maintenance.md))
  all ship and are covered by tests (`test_plugins.py`, `test_client_pool.py`,
  `test_doctor_endpoint.py`). The extension model is *additive*: plugins dock onto a stable
  contract, the core is never patched from outside (boundary-check enforced).
- **In development — our internal release machinery.** The **three-stage DEV → Prod →
  Public promote pipeline** (a private dev fork that builds+tests in Docker → merge into the
  private source on a green artifact → gated public export) and its eventual **automated
  evening sync** are core-maintainer machinery, not yet a one-command flow. Until it lands,
  promotion is the existing **manual** gated path (boundary + tests + docs + review +
  export). Downstream users never touch any of this — they pull the framework and extend it
  over the plugin API, or fork it freely (Apache-2.0).

So the way you *extend* Ironclad is shipped and tested; the way *we harden our own
releases* is partly manual and being formalized.

## Reference load test

Run against the full reference stack on the DGX Spark (2026-06-17): all five
containers up — `vllm-35b`, `mem-api`, `mem-qdrant`, `mem-neo4j`,
`ironclad-orchestrator` (all healthy) — driven from a workstation over the LAN.
*(The warm tier `mem-valkey` was added after this run — the current `--profile memory`
stack is six containers.)*

**Fan-out (8 independent reasoning prompts, concurrent):**

| metric | value |
|--------|-------|
| success | 8 / 8 |
| wall-clock | 1.2 s |
| Σ per-prompt latency | 7.1 s |
| **speedup vs serial** | **5.8×** |
| aggregate throughput | ~118 tok/s |

**Chat (3 sequential turns, single agent — serialized by design):**

| metric | value |
|--------|-------|
| mean turn latency | 2.1 s |
| decode rate | ~55–68 tok/s |
| answers | all correct, in the configured language (German) |

Interpretation: the parallel reasoning path scales near-linearly to the model's batch
width (`max_num_seqs=8`); the conversational path is intentionally one-turn-at-a-time
behind the agent lock. Both are healthy on a single GB10.

## Sealed channel — live verification (Phase d)

Verified on the reference GPU (2026-06-17) against a server in the `sealed` profile,
driven from a workstation over a **real SSH local-forward** (the client-managed tunnel),
without touching the production orchestrator:

| check | result |
|-------|--------|
| server bind | `127.0.0.1:8101` only (loopback — never the LAN) |
| `/health` | `sealed: true`, `security.profile: sealed` |
| `/tasks`, `/session/open` without the secret | **401** |
| `/session/open` with the secret → `/tasks` with secret + session | **200** |
| real model turn (`/chat`) through the sealed tunnel | answered correctly |
| after `/session/close` → `/health`, then `/tasks` with the stale session | `sealed: true`, **401** |

The channel is open exactly while a live session exists and seals the moment it ends —
OS-enforced when the tunnel closes, app-enforced by the session heartbeat. Single-tenant
throughout: the secret authenticates the deployment, not a user.
