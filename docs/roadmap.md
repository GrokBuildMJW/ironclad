# Roadmap

> Honest and explicit: this separates **what works today** from **what is planned**.
> For the per-component wiring status of shipped features, see
> [`status.md`](status.md). Treat `main` as a development snapshot.

## Where Ironclad is today (single-tenant, home-LAN trust)

Ironclad currently runs as a **single-operator** system:

- **One principal.** There is exactly one identity — the operator running the
  deployment. The TaskStore, the memory namespace, and every tool run in that one
  context. There is **no multi-user authentication or authorization** yet.
- **Home-LAN trust transport.** The orchestrator server is reached like the model
  port: plain client-initiated HTTP on a trusted network, **no auth**. Code locality
  is structural — the client pulls handovers and runs the code-agents on its own
  machine; project code never has to leave it.

This is a deliberate, honest starting point that matches a **sovereign / local
deployment**: your box, your model, your data, one operator. It is *not* yet safe to
expose to untrusted networks or to share between users.

## Phase d — secure, session-gated channel (done; single-tenant)

Hardening the PC↔server channel for a single operator, **without** pretending to be
multi-user. Built, unit-tested end-to-end, **and live-verified** over a real SSH tunnel
on the reference GPU (loopback bind, gated routes, session open→live→close, a real model
turn through the sealed channel — see `status.md`).

- **Selectable trust profiles** (`security.profile`): `open` (today's behaviour —
  out-of-the-box, no auth, mount allowed), `token` (shared deployment secret over the
  LAN), `sealed` (localhost-bind behind a client-managed tunnel + deployment secret +
  session heartbeat).
- **Explicit session lifecycle.** The client opens a session, heartbeats it, and
  closes it on exit; when no live session exists the server **seals** — client-facing
  endpoints refuse, and background planning pauses.
- **The token is a *deployment secret*, not a user login.** It proves "this is my
  client process," nothing more. Because there is still exactly one principal, this
  adds no per-user scoping and makes no multi-tenant promise.
- **Code-locality as policy, not a hard ban.** `open`/`token` allow a code mount if
  you want it; `sealed` enforces pull-only, code-stays-local.

## Planned — Phase g: Identity & Authorization (multi-tenant)

Real multi-user, enterprise/government deployments need far more than a token, and
**none of it exists yet** — listing it honestly rather than implying it ships:

- A **principal/scope** threaded through the whole engine: TaskStore ownership,
  per-principal **memory namespaces** (so one user's memory never bleeds into
  another's), and **entitlement-scoped data sources**.
- **Organisation structures and groups** — role/attribute-based access (RBAC/ABAC)
  driven by identity-provider claims (OIDC / SAML).
- Audit trails and per-tenant isolation guarantees.

Until Phase g lands, treat any "enterprise/government" use as **single-tenant on
trusted infrastructure**. Multi-tenant identity is a direction, not a feature.

## Phase e — governed parallelism (done; single-tenant)

Server-side reasoning parallelism the orchestrator actually uses, made **operationally
safe** so it can't crash a local GPU. A config-driven concurrency cap plus a token-budget
envelope (`concurrency × max_tokens ≤ max_batch_tokens`) means a large per-call token
count lowers parallelism rather than over-subscribing — overflow simply queues. Core
ships conservative, model-agnostic defaults; the private deploy pins the model-matched
values. Exposed both as the `/fanout` endpoint and the in-engine `parallel_reason` tool.
Unit-tested and live-verified (8/8 at ~5.8× on the reference GPU).

## Extend it through itself — open plugin surface (done) + DEV/Prod self-maintenance (in development)

Ironclad is designed to be **extended and maintained through itself**, with a clean
separation between a closed, curated core and an open extension surface.

**Shipped and tested — how you extend it:**

- **One open, versioned plugin contract.** Drop a Python file (`CASE` dict + `run(...)`)
  into a `skills/` directory, point `GX10_PLUGINS_DIR` at it, and the engine discovers it
  and exposes it as an agent tool — **no core fork, no core patch** (the boundary check
  enforces that plugins never reach into `core/`). See [`plugin-api.md`](plugin-api.md).
- **Bring your own code-agent CLI** (`GX10_AGENT_CMD`) — the local coding agent is a command
  template, not hard-wired to Claude Code. See [`code-agents.md`](code-agents.md).
- **A reproducible dev container** that builds the engine and runs the **full suite inside
  Docker** (the build+test gate), see [`dev-environment.md`](dev-environment.md), plus a
  runtime `GET /doctor` contract self-check.
- **A beginner on-ramp** — [`self-maintenance.md`](self-maintenance.md): *describe an idea,
  let the agents build it*; build a plugin (Mode A) or repurpose the whole framework for
  yourself (Mode B); report a bug → reproduce → fix → ship.

**In development — our internal release machinery (you don't need it).** A **three-stage
DEV → Prod → Public promote pipeline** (a private dev fork builds+tests in a container → a
green artifact gates the merge into the private source → a gated public export) and an
eventual **automated nightly sync** harden *our own* releases. It is being formalized; today
promotion is the existing **manual** gated path (boundary + tests + docs + review + export).
The core stays **inbound-closed**: we maintain it ourselves and never merge outside changes
into it — the only inbound is a bug *report* (we reproduce → fix → ship with a regression
test). Downstream users never touch this; they pull a finished, verified framework and either
extend it over the plugin API (Mode A, inherits our updates) or fork it freely under
Apache-2.0 (Mode B, autonomous).

## Shipped — the recommended terminal client

The **recommended** client is now a **TypeScript terminal client** (`clients/ink/`),
**bundled in the repo** and built from source. It is built for a polished, responsive feel
that does **not** hinge on model latency — the client stays snappy while the backend works:

- A **purpose-built terminal renderer** (own React reconciler + flexbox layout, packed
  cell buffer, cell-level diffing) for **ghost-free resize**, smooth token streaming, and
  native-grade **scrollback, selection and copy** — addressing the rendering limits of
  off-the-shelf TUI stacks. Built clean-room from public patterns and MIT references.
- The orchestrator core stays untouched: the client speaks the same HTTP/tool-bridge
  contract (local code-tools still run on your machine). Build it once
  (`npm install && npm run build` in `clients/ink/`; needs Node ≥ 22) — see the README and
  SETUP. The Python line REPL (`engine/client.py`) and full-screen TUI (`engine/tui.py`)
  remain as **legacy** zero-dependency fallbacks.

## Planned — scalable-context memory (context extension + short-term memory)

Today's memory hook is deliberately minimal: it stores *completed tasks* and injects a
flat top-K recall at stage time, while the live turn is kept inside the model window by
char-based trimming that simply **drops** the oldest rounds. The planned extension turns
that into a proper **multi-tier context system**, so the *total addressable* context far
exceeds the bounded model window — without making decode any slower.

**The key insight — two layers, and only one is cheap to grow.** The model's context
**window** is a hard per-request budget bounded by VRAM and memory bandwidth; growing it
makes *every* token slower, so it is raised only modestly and only as a last step. The
"effectively unbounded" context instead comes from the **retrieval layer**, which grows
freely on disk. The window stays a small *working set*; the scaling lives in memory.

**Three tiers:**

- **Hot — the model window (working set).** Recent verbatim rounds + the active task
  spec. Stays bounded so decode stays fast. Raised (if at all) only as a hardware-gated
  last step, after the retrieval layer has proven it carries the load.
- **Warm — short-term memory (new).** A fast in-memory tier holding the **rolling
  conversation summary**, recent-turn state, and a short-TTL **retrieval cache** in front
  of the long-term store. Unlike the window it **survives a server restart** and is
  **shared across the parallel reasoning workers** (which today share no history at all).
  Reads are cache-aside under a hard time budget — a miss just falls through to today's
  path, never a stall. (Built on a BSD-licensed in-memory store, so it stays OSS-clean.)
- **Cold — long-term memory (exists today, gets richer).** The vector(+graph) store that
  already ships. Planned: it also receives the **summarized + chunked** evicted context
  (lossless — the raw text is archived even where the summary is lossy), so nothing the
  agent has seen is ever truly discarded.

**Planned mechanisms — all additive, flag-gated, and fail-soft; the hot read path stays
vector-only and off the timeout-prone graph path:**

1. **Rolling / hierarchical summarization on eviction.** When a round would be trimmed, it
   is first summarized into a compact running block kept just below the system prompt and
   archived raw to long-term memory — instead of dropped. The post-system prefix stays
   byte-stable so the model's prefix cache survives across turns.
2. **Per-turn retrieval (RAG) assembly.** Before each turn, a bounded vector lookup on the
   user's message injects a token-budgeted "relevant context" block — recall on *every*
   turn, not only at stage time. This is what makes the total context effectively
   unbounded.
3. **Token-accurate budgeting** that reserves room for the output, the retrieved block and
   the summary, replacing today's char thresholds (the hysteresis is kept).
4. **Chunked, lossless long-artifact store** so large documents become retrievable
   passage-by-passage without ever entering the window whole.
5. **Parallel workers as equal memory citizens.** The reasoning-worker fan-out gains the
   same shared summary + per-item retrieval on **read**, and a single-writer reducer
   consolidates their outputs into one de-duplicated **write** — so the workers contribute
   to and draw on memory like the main loop, without a write race.

Flag-off is byte-identical to today, and with the warm/cold tiers down a turn still
completes (fail-soft); the model stays the only thing that can block a turn. None of this
needs the client — "more context" is entirely server-side: same HTTP contract, no new
endpoint, no version coupling.

## Also planned

- Broaden test coverage; harden the new server/client paths.
- Verified connection recipes for more locally-served open models.
- Retrieval / **RAG over local datasets** through the memory hook.
- First tagged release once the APIs settle.

Issues and discussions are welcome — this is an early, openly-developed project.
