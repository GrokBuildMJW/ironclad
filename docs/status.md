# Project status & wiring

> ## ⚠️ Read this first
>
> Ironclad's engine grew out of a **proven, in-production orchestrator** (years of
> daily use). It is currently undergoing a **complete redesign**: the original
> single-process CLI is being split into a headless **server** + thin **client**,
> containerized, and given a reasoning-worker fan-out and a full-screen TUI.
>
> **Not everything is re-wired or re-tested yet.** The pieces below are marked
> honestly: *proven* (inherited, battle-tested), *wired + tested* (rebuilt and
> verified in the new architecture), *placeholder* (hook exists, backend/logic not
> yet ported), or *opt-in* (off by default). Treat `main` as a development snapshot.

This document is the single source of truth for **what actually works right now**.

## Module reference

| File | What it does |
|------|--------------|
| `engine/gx10.py` | The orchestrator engine **and** the original monolithic CLI. Agent loop, tool execution, deterministic `TaskStore`, fail-closed macros (`stage_handover` / `advance_pipeline`), config-tree loader, platform detection, context trimming. This is the *proven* core. |
| `engine/server.py` | **Headless server** (new). Drives the engine with no UI, exposes plain-HTTP endpoints, serializes agent access, runs the feedback-side reconciler. |
| `engine/client.py` | **Thin client** (new). Line REPL + the local code-agent pool (`claude --print`) that keeps project code on your machine. |
| `engine/tui.py` | **Full-screen client** (new). prompt_toolkit UI, live streaming, status toolbar, scrollback, compressed multi-line paste. |
| `engine/commands.py` | Shared command router for REPL + TUI (`/command` → local or forwarded to the server). |
| `engine/workers.py` | **Reasoning-worker fan-out** (new). Independent prompts run concurrently against the model (`POST /fanout`). |
| `ack/` | **Agent-Contract-Kernel**: schema-as-SSOT (`case_spec`), bounded validate→reask (`validated_emit`), constrained emission (`constrained_emission`), registry, doctor, generator, and the opt-in `lodestar` plugin. |

## Wiring status

| Component | Status | Notes |
|-----------|--------|-------|
| Agent loop, tools, TaskStore, macros | **proven** | Inherited from the in-production orchestrator. |
| Config tree + `GX10_*` env + language setting | **wired + tested** | `language` (reply language) default `en`. |
| Server/client split (`/chat`, `/chat/stream`, `/tasks`, `/pending`, `/feedback`) | **wired + tested** | Verified PC→LAN→Spark; headless capture; streaming. |
| Thin client + parallel code-agent pool | **wired + tested** | Bounded pool; claim-once; unclaim-on-failure. |
| Full-screen TUI (stream, toolbar, scroll, paste) | **wired + tested** | Render/scroll/paste unit-checked; runs in a real terminal. |
| Reasoning-worker fan-out (`/fanout`) | **wired + tested** | Concurrency bounded by `max_num_seqs`; measured speedup. |
| ACK contract gate at `stage_handover` | **wired + tested** | Soft path (`_ack_validate`): task_json validated against `TaskSpec`, fail-closed → reask. |
| Orchestrator Docker image / compose | **wired + tested** | Runs as `ironclad-orchestrator` next to the model. |
| **Memory (Mem0)** | **wired + tested** | `engine/memory.py` talks to a Mem0-style service (`GX10_MEMORY_URL`); store+search verified live. The store starts **empty** — see below. |
| **Autoplan** (`/autoplan on\|off [N]`) | **wired (config-gated)** | Ported into the server's queue consumer (`_autoplan_tick`), **decoupled from autopilot** so it works in the split: server plans → client executes → server advances → server plans again. Fires only when `/autoplan on` is set **and** `paths.active_capability_backlog` is configured (no backlog → it disables itself). Logic unit-tested. |
| Autopilot auto-launch on the server | **placeholder / by design off** | The server never launches code-agents (`_LAUNCH_CMD` is skipped); launching is the client's job (the pool). The server-side `autopilot` toggle is currently inert. |
| Remote turn cancel (Ctrl+C in the TUI) | **wired + tested** | `POST /cancel` sets the engine cancel event; the running turn aborts at its next iteration. Ctrl+C in the TUI fires it non-blocking. |
| Constrained-emission **hard floor** (grammar) | **available — soft path active by design** | Grammar-constrained decoding (guided JSON) is **verified stable** on the reference GPU (no engine crash, schema-valid output). The ACK exposes it (`constrained_emission` / `emit_validated`) for callers wanting token-level guarantees; the **orchestration engine deliberately keeps the soft validate→reask gate** — it's backend-agnostic (any OpenAI endpoint) and already ~100% reliable, so per-emission grammar buys little. Not a TODO — a decision. |
| **Lodestar** capability→backlog plugin | **opt-in (off)** | `lodestar.enabled=false` by default; demo in `examples/demo-vessel/`. |
| **Security / trust model** | **home-LAN trust (single-tenant)** | Today the server trusts its network like the model port — **no authentication**, one operator, one principal. A selectable, session-gated, authenticated channel (still single-operator) is **in progress** (Phase d). **No multi-user identity/authorization** exists — see the [roadmap](roadmap.md). |

## Memory

The engine has long-term memory **wired in**: a `query_memory` tool, store-on-task-
completion, and stage-time context injection, backed by `engine/memory.py` — a small,
secret-free client for a **Mem0-style HTTP service** (`POST /add`, `POST /search` with
`graph=false`, `GET /health`). Store + search are verified live against the reference
Mem0 stack (Qdrant + Neo4j + BGE-M3, LLM pointed at the local model).

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
   `POST /cancel` + Ctrl+C in the TUI.
4. ~~Decide the grammar hard-floor.~~ **Decided** — verified stable on the reference
   GPU; available via the ACK; the engine keeps the soft path by design (see table).

The rebuild's known placeholders are now resolved. What remains is ordinary
hardening and breadth of testing — treat `main` as a development snapshot still.

## Reference load test

Run against the full reference stack on the DGX Spark (2026-06-17): all five
containers up — `vllm-35b`, `mem-api`, `mem-qdrant`, `mem-neo4j`,
`ironclad-orchestrator` (all healthy) — driven from a workstation over the LAN.

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
