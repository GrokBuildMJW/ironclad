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

> **Release model.** Ironclad is **pre-release** (`0.0.x`, alpha). Tagged releases are
> published on **PyPI** as `ironclad-ai` and as **GitHub Releases** (latest `v0.0.20`);
> the importable wheel is the Agent-Contract-Kernel (`ack` + `ack.lodestar`), while the
> orchestration engine ships as runnable scripts until its API stabilizes. There is **no
> stable/1.0 release yet** — APIs, layout, and config may change between `0.0.x` versions.

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
| **Runtime config control** (`/config get\|set <dotted.key> <value>`) | **wired + tested** | Generic, plugin-agnostic read/override of the live config tree without a restart: `set` writes the dotted key and re-derives engine globals via `_apply_config`; keys core doesn't model (e.g. plugin sections) are stored and re-read by their owner on next use. Process-local (not persisted). See [`config-runtime.md`](config-runtime.md). |
| **Setup types** (`setup.type`, boot-fixed operating mode) | **wired + tested** | A boot-only, runtime-frozen config key selecting where ironclad runs — orchestrator + agents always co-located (no cross-machine offload): `server` (default — everything on the model host, in-engine only, byte-identical) or `local` (engine + agents native on the user's machine; the model + **Cold** memory (Mem0) stay remote, the **Warm** cache (Valkey) follows the orchestrator — #385; offload = local subprocess, requires a remote `base_url` + a reachable CLI). Fail-closed on a mode that can't be honored; `sealed` forces `server` (no egress). See [`setup-types.md`](setup-types.md). |
| **Orchestrator system prompt** (`engine/prompts/`, `GX10_PROMPT`) | **shipped** | A generic, secret-free default orchestrator system prompt ships in `engine/prompts/GX10_Orchestrator_SystemPrompt.md` (role, tool-use discipline, the fail-closed macros, the workflow). A deployment overrides it with a project-specific prompt via `GX10_PROMPT` (kept private, never in `core/`). |
| **Orchestrator version stamp** (`orchestrator_version`) | **wired + tested** | The engine reads an opaque build identity — `GX10_ORCHESTRATOR_VERSION` → else `engine/VERSION` (gitignored) → else `"unknown"` — and exposes it in `/health` and the boot line. Core only reads and shows it (no git/SHA logic); a deployment stamps the same value into every target so co-located builds are verifiably drift-free. |
| **Provider-router / dispatcher** (`engine/providers.py`, `router.py`, `dispatch.py`) | **wired + tested — off in `server`** | Backend registry + routing policy (effort·sovereignty·load·budget) + governed dispatch that wraps `parallel_reason` with fail-soft spill. Boot-derived from `setup.type`: **disabled in `server`** (default — `providers_enabled=False`, byte-identical in-engine path), enabled in `local`. Off by default; deterministic unit tests (`test_dispatch`, `test_router`, `test_providers`, `test_artifact_routing`). **First-class `web_search`** (epic #505): a strict `{query, allowDomains?, blockDomains?}` tool (validated in a pure validator; schema grammar-clean) running through a **vendor-neutral adapter seam** (`search.adapter`: `cli` delegate · `brave` **native HTTP** on stdlib `urllib`, **local-only** · `mock`) **independent of this lane**, so a native-search deployment with no CLI provider still offers it. Returns structured results + `durationMs`, capped, with a **deterministic `Sources:`** block; a `[search]` stream sentinel drives a "web N · Xms" footer chip in every client. **Trust-gated**: blocked under the `sealed` profile (operator opt-in `security.web_in_sealed`), enforced at the offer- AND exec-gate, and kept out of `LOCAL_TOOL_NAMES`. A conservative EN+DE current-info classifier proactively steers "latest/today/aktuelle Lage" requests to it, and a **fail-closed `execute_command` guardrail** refuses a remote/web fetch or an unbounded/progress-emitting process (redirecting web fetches to `web_search`) + hardens PowerShell with `$ProgressPreference='SilentlyContinue'`. The search key (`GX10_SEARCH_API_KEY`) is name-indirected from the environment; the optional CLI backend is a private `conf/` detail. See [`web-search.md`](web-search.md). **SOFT distinct-reviewer anti-affinity** (#457): `RouteRequest.excluded_provider_ids` (caller-passed — `route_one` stays pure) drops the artifact's producer from the candidates so a review-of-a-review never routes back to its author while an equal peer remains; if excluding it would leave no capable agent the producer is kept (waived, never declined). `RouteDecision.distinct_reviewer` records `applied`/`waived`/`None`; HARD sovereignty/budget outrank it (a SENSITIVE request stays local rather than leaking to a "distinct" external). A private CI invariant `review-distinct-reviewer` asserts the seam stays intact. |
| Server/client split (`/chat`, `/chat/stream`, `/tasks`, `/pending`, `/feedback`, `/tool-result`, `/cancel`, `/doctor`, `/session/*`) | **wired + tested** | Verified PC→LAN→Spark; headless capture; streaming; `/tool-result` is the local code-tool passthrough. |
| Thin client + parallel code-agent pool | **wired + tested** | Bounded pool; claim-once; unclaim-on-failure (in both the TypeScript and Python clients). |
| **TypeScript terminal client** (`clients/ink/`) | **wired + tested** | Purpose-built renderer (React reconciler + Yoga flexbox + packed cell buffer + cell-level diff). **344** `node:test` cases (340 passing + 4 skipped) across renderer/UI/net/tools; ghost-free resize, native scrollback/selection/copy, live-streaming markdown with preserved + syntax-highlighted code, slash-command autocomplete, `!cmd`, in-CLI `/update`, per-project session; adversarially verified. Build-from-source / global install (Node ≥ 22). |
| Full-screen Python TUI (stream, toolbar, scroll, paste) — **legacy** | **wired + tested** | Render/scroll/paste unit-checked; runs in a real terminal. Superseded by the TypeScript client. |
| Reasoning-worker fan-out (`/fanout`) + `parallel_reason` tool | **wired + tested** | Stateless concurrent reasoning, **GPU-safety-governed**: a config-driven concurrency cap **and** a token-budget envelope (`concurrency × max_tokens ≤ max_batch_tokens`) so a large `max_tokens` lowers parallelism instead of over-subscribing the box — overflow just queues. Core ships conservative defaults (concurrency 4); the deploy pins model-matched values (8 = our model's `max_num_seqs`). Now also exposed **in-engine** as the `parallel_reason` tool, so the orchestrator itself batches independent items — **server-only** (it needs the server-constructed governed workers; in a plain library/CLI import it reports "unavailable"). Live-verified: governed `/fanout` 8/8 at ~5.8× speedup on the reference GPU. |
| **Initiative-centric state layout** (`vault/<slug>/`, `.ironclad/`) | **wired + tested** | Engine machinery is hidden under `state_root` (`.ironclad/`: `session.json`, warm-cache, `active` marker); every produced artifact lives under the **active initiative** `vault/<slug>/` (visible `decisions/ proposals/ reviews/ runs/ tasks/`, hidden `.work/` plumbing). Explicit creation (`/initiative new <name> --type mpr\|software`); the **engine-routed** artifacts — the `TaskStore`, the `stage_handover`/`advance_pipeline` plumbing, and MPR `runs_dir` — resolve under the active initiative (**fail-closed** with none active; background scanners soft-skip), while `decisions/`/`proposals/`/`reviews/` are seeded dirs the agent writes into. Project root stays clean. See [`state-and-initiative.md`](state-and-initiative.md). |
| **Self-maintaining vault** (`reconcile_vault`, `/initiative reconcile`) | **wired + tested** | Deterministic, **LLM-free**: regenerates `INDEX.md` (grouped, Obsidian `[[links]]`, manual prose preserved) and injects an idempotent "Verwandt (auto)" relation block into curated docs (shared tags / title reference). Auto-fires index-only after writes; full link pass on the explicit command. |
| ACK contract gate at `stage_handover` | **wired + tested** | Soft path (`_ack_validate`): task_json validated against `TaskSpec`, fail-closed → reask. |
| Function-calling robustness (every tool boundary) | **wired + tested** | Validate→reask on **all** tool arguments, not just task_json: malformed JSON or a schema violation (required + types) is returned to the model as the tool result so it re-emits — never silently degraded to empty args. Live-verified end to end (native tool turn against the reference model). |
| Model-agnostic tool-call recovery | **wired + tested** | When an OpenAI-compatible endpoint returns no native `tool_calls`, calls the model emitted as **text** (`<tool_call>` tags, fenced json, or a bare object) are recovered — gated to known tool names so a legitimate JSON answer is never hijacked. Removes the hard dependency on a server-side tool parser. |
| Orchestrator Docker image / compose | **wired + tested** | Runs as `ironclad-orchestrator` next to the model. |
| **Memory — multi-tier** (Mem0 cold + warm tier + per-turn context pipeline) | **wired + tested** | Cold: `engine/memory.py` → Mem0-style service (`GX10_MEMORY_URL`), **model-host-pinned** (GPU/LLM-coupled). Warm: `engine/warm.py` → a Valkey/BSD store (`GX10_WARM_URL`, fail-soft) that **follows the orchestrator** (loopback ideal, a LAN hop to the model host acceptable — #385/Decision D-Valkey). `/health` reports **Cold and Warm separately** (`memory` + `warm` = up/down/off), so a silent Warm outage can't hide behind a Cold-only `memory: up` (#385); the Ink footer shows both. Plus rolling summarization, per-turn RAG (with warm cache-aside), token-accurate budgeting, chunking + recency, the `deep_query_memory` (graph) tool, and worker memory (shared summary on read + single-writer reduce). A staged handover gets a **token-budgeted Memory brief** (`MemoryManager.brief`, #458 / FORK-G D1): the shared warm rolling summary + **body-keyed** vector hits + optional relational hits, deduped and trimmed to `context.memory_brief_tokens` (default 1200), fail-soft — so an external coding CLI starts a task with the orchestrator's memory. Default-on where a memory/warm service is configured; store starts **empty** — see below. A **read-only Memory MCP** (`memory_mcp.py`, #480 / FORK-G D2) additionally lets an MCP-capable CLI (Codex/Claude) LIVE-query the project memory (`memory_search`/`memory_deep_query`, no write) during a handover — a dependency-free stdio JSON-RPC server the CLI spawns, **gated on the `sealed` trust profile**, **secret-free** (the connection travels in the spawned env, not the wire), scoped to the **project namespace**. |
| **Autoplan** (`/autoplan on\|off [N]`) | **wired (config-gated)** | Ported into the server's queue consumer (`_autoplan_tick`), **decoupled from autopilot** so it works in the split: server plans → client executes → server advances → server plans again. Fires only when `/autoplan on` is set **and** `paths.active_capability_backlog` is configured (no backlog → it disables itself) **and** the channel is not sealed (under `sealed`, with no live session, autoplan pauses: `[AUTOPLAN] paused — channel sealed`). Logic unit-tested. |
| Autopilot auto-launch on the server | **placeholder / by design off** | The server never launches code-agents — its queue consumer drops the internal `_LAUNCH_CMD`; launching is the client's job (the pool). `autopilot.enabled` is itself a runtime-settable toggle, but it has **no launch effect on the server** (it only makes the reconciler enqueue launches the server then discards). When the autopilot *does* run (the local/desktop path), its per-agent logs go to `state_root()/logs` (`.ironclad/logs`), not the project root. |
| Remote turn cancel (Esc / Ctrl+C) | **wired + tested** | `POST /cancel` sets the engine cancel event; the running turn aborts at its next iteration. In the recommended TypeScript client **Esc or Ctrl+C** cancels — it aborts the stream locally (instant return to idle) and POSTs `/cancel`; the legacy Python TUI fires it via Ctrl+C non-blocking. |
| Constrained-emission **hard floor** (grammar) | **available — soft path active by design** | Grammar-constrained decoding (guided JSON) is **verified stable** on the reference GPU (no engine crash, schema-valid output). The ACK exposes it (`constrained_emission` / `emit_validated`) for callers wanting token-level guarantees; the **orchestration engine deliberately keeps the soft validate→reask gate** — it's backend-agnostic (any OpenAI endpoint) and already ~100% reliable, so per-emission grammar buys little. Not a TODO — a decision. |
| **Lodestar** capability→backlog plugin | **opt-in (off)** | `lodestar.enabled=false` by default; demo in `examples/demo-vessel/`. |
| **Security / trust model (Phase d)** | **wired + tested (single-tenant)** | Selectable trust profiles (`security.profile`): `open` (default — no auth, LAN bind, mount allowed), `token` (deployment secret over the LAN), `sealed` (loopback bind behind a client-managed tunnel + secret + session heartbeat; client-facing endpoints refuse and autoplan pauses when no session is live). The token is a **deployment secret, not a user login**. Fail-closed: an auth profile refuses to boot without a secret. Unit-tested end-to-end (no vLLM) **and live-verified** over a real SSH tunnel on the reference GPU (loopback bind, gated routes 401, session open→live→close, a real model turn through the sealed channel — see below). **No multi-user identity/authorization** — single principal only; see the [roadmap](roadmap.md). Config/env: `security.profile`/`GX10_PROFILE` (boot-only/frozen), the secret in `GX10_SERVER_TOKEN` (env named by `security.token_env`), `security.session_heartbeat_s`/`GX10_SESSION_HEARTBEAT`, and `security.code_locality`/`GX10_CODE_LOCALITY` (`mount`\|`local` — honored under open/token, forced `local` under sealed, advertised on `/health`). The same gate applies in **`token`** (401 without the Bearer secret, no session needed) and **`sealed`** (also needs a live session). Operator how-to: [`security.md`](security.md). |
| **Core built-in loader** (always-on) | **wired + tested** | Built-in skills/prompts load at startup from a **fixed core dir (`skills/`)**, scanned **unconditionally** — independent of `GX10_PLUGINS_DIR` (`_load_skills`). Built-ins work out of the box, no config. Covered by `test_builtin_loader.py` (4). ADR-0002 #114. |
| **Open plugin surface** (`GX10_PLUGINS_DIR`) — 3rd-party | **wired + tested** | The open, versioned extension contract for **3rd-party/user** skills: drop a Python file with a module-level `CASE` dict + `run(...)` (or a `SKILL.md` playbook) into a `skills/` directory, point `GX10_PLUGINS_DIR` at it, and the engine **discovers it additively — with no core change** (built-ins are no longer routed through it). The CI boundary check keeps `core/` standalone + secret-free; it does **not** sandbox plugins — a plugin runs in-process. See [`plugin-api.md`](plugin-api.md). |
| **Extension SDK** (`ack.sdk`) — separate-repo authoring | **wired + tested** | The **curated, versioned import surface** a plugin builds against from its **own repo** (`pip install ironclad-ai`): `ack.sdk` re-exports the tool/playbook/prompt kinds, the registration/eval `gate`, `derive_tool_schema`, `Localizer`, and the `catalogue` — `ack.sdk.__all__` **is** the public API (everything else under `ack.*`/`engine.*` is internal). Provisional while `0.0.x`, semver from 1.0. A shipped **example plugin** ([`examples/example-plugin/`](../examples/example-plugin/)) shows the separate-repo shape, and the **clean-room builds it against the freshly-installed wheel** (proving a separate repo can build against the published artifact). Covered by `test_sdk.py` (7) + `test_example_plugin.py` (3). [ADR-0004](adr/0004-extension-sdk.md), [`plugin-api.md`](plugin-api.md). |
| **Packaged-plugin loading** (`ironclad.plugins` entry-point) | **wired + tested** | A *packaged* plugin (pip-installed into the deployment) is discovered at startup via the `ironclad.plugins` **entry-point group** — additively alongside built-ins + `GX10_PLUGINS_DIR`, with **no path config and no core change**. Dependency-inverted: the engine resolves each entry point to a plugins dir (package / callable / path) and scans it; it **never imports a concrete plugin** (only the generic group string couples them). Broken entry points are fail-soft. An **export-leak guard** (boundary + export forbidden-literals + `test_export_leak_guard.py`, 4) keeps any **internal** plugin out of `core/` and the public export — the coupling is only the generic group string. Covered by `test_entrypoint_loader.py` (10). [ADR-0004](adr/0004-extension-sdk.md). |
| **Playbook skill kind** (`SKILL.md` + `use_skill`) | **wired + tested** | The second skill kind (ADR-0001): `ack.playbook` parses+validates `SKILL.md` packages and `Registry.discover_playbooks` finds them alongside the typed-tool `discover_skills`; the engine exposes them via the `use_skill` tool with **progressive disclosure** (list metadata → load body → load a reference on demand). Covered by `test_playbook.py` (15). See [`skill-packaging.md`](skill-packaging.md). |
| **Skill generator** (`ack.skillgen`) | **wired + tested** | Spec → a schema-valid scaffold for **both** kinds (typed `CASE`+`run` `.py` with a derived tool schema + an auto-test stub; or a `SKILL.md` playbook package + `references/` + `scripts/check`). Contract-correct by construction; the body is a marked stub for an author/LLM to fill (ADR-0001 D3). The richer paved-road domain scaffold remains `ack.generator`. Covered by `test_skillgen.py` (7). |
| **Skill library catalogue** (`ack.catalogue`) | **wired + tested** | Self-hosted manifest index over **both** kinds (reads the skill's own metadata — no separate registry file to drift): `capability`/`kind`/`version`/`type`/`domain`/`provenance`/`source`, semver compare, discover + install (copy) + update-when-newer, built-in vs user libraries. No external marketplace. Covered by `test_catalogue.py` (6). |
| **Skill registration gate** (`ack.gate`) | **wired + tested** | No unchecked skill registers: a **tool** must pass a doctor preflight (loadable, `CASE`+`capability`, **sync** `run`, derivable schema) and ship an auto-generated test; a **playbook** must have valid `SKILL.md` frontmatter + readable references + a passing `scripts/check`. Behavioral `eval/` stays opt-in. Covered by `test_gate.py` (7). |
| **Shared content i18n** (`ack.i18n`) | **wired + tested** | Core file-overlay locale loader (`Localizer(locales_dir)`): English source + `<lang>.json` overlay along a dotted path, English fallback; **flag-independent** (always importable, no `GX10_MPR`/plugin coupling), **parameterized locales dir** (each skill/prompt points at its own). MPR migrated onto it (its `i18n.py` is now a thin shim; 381 green). Distinct from `engine/messages.py` (engine chrome). Covered by `test_i18n.py` (6). Part of the core-always-on rebuild (ADR-0002, #112). |
| **Prompt library & generator** | **wired + tested** | Epic #105 on the core base ([ADR-0003](adr/0003-prompt-library.md) + [`prompt-packaging.md`](prompt-packaging.md)). The `kind: prompt` item format (`ack.prompt`, `test_prompt.py` 7); **multilingual assembly** (`ack.promptgen.assemble` — template + values → target language via `ack.i18n`, source/target + fallback; `test_promptgen.py` 6); **slash surface + guided elicitation** — a discovered prompt is exposed as the `use_prompt` engine tool (list → ask-next-required-question → assemble + preview, `lang`-aware), wired into `_load_skills`/`_effective_tools`/dispatch (`ack.promptgen.run_prompt`; `test_prompt_cmd.py` 9, #110); **eval/registration gate** for prompt items (`ack.gate.gate_prompt` — required vars must appear in the template, every declared language must assemble cleanly, overlays validated; `test_gate.py`) and a **curated multilingual starter library** of 7 built-ins under `skills/prompts/` (`code-review`, `commit-message`, `bug-report`, `explain-code`, `pr-description`, `refactor-plan`, `test-plan`; EN+DE; `test_prompt_library.py` 24, #111/#150). **New prompt = drop an MD file** — no engine change. |
| **Discovery commands** (`/prompts`, `/skills`) | **wired + tested** | Read-only listing of the **one loaded registry** — no re-scan, no parallel mechanism. `/prompts` lists every loaded `kind: prompt` item (name, declared languages, description); `/skills` lists every loaded skill across both kinds — `SKILL.md` playbooks **and** typed `CASE`+`run` tools (incl. the MPR built-in). Both are server commands (forwarded by the clients), advertised in `/help`, and offered in the TypeScript client's slash autocomplete. Backed by `_catalogue_snapshot` over `_PROMPTS`/`_PLAYBOOKS`/`_PLUGIN_TOOLS`. Covered by `test_discovery_cmds.py` (6). |
| **Catalogue endpoint + dynamic autocomplete** (`GET /catalogue`) | **wired + tested** | A guarded read-only endpoint serving the loaded registry snapshot (same `gx10._catalogue_snapshot`, one surface) — gated like `/tasks`/`/doctor` (in `GATED_PATHS`; 401 without the deployment secret under token/sealed). The TypeScript client fetches it lazily on the first slash-menu open and merges the loaded **prompt** names into slash autocomplete as directly-invocable `/<name>` entries (a built-in command wins on a name collision; skills are discoverable via `/skills` but not injected as dead completions). Fail-soft: no `/catalogue` (older server) or a gated/closed session → built-in commands only. Covered by `test_catalogue_endpoint.py` (3) + `catalogue.test.ts` (6). |
| **Prompt invocation** (`/<prompt-name>`) | **wired + tested** | Deterministic, model-free per-item invocation: the command router resolves `/<prompt-name>` against the loaded prompt catalogue **after** every built-in command (so a real command never shadows), parses `var=value` / a single positional / `--lang xx`, and runs the `ack.promptgen` elicitation state machine — assembling the finished prompt in the target language when all required vars are present, else returning the guiding questions for what is missing. The model-elected `use_prompt` tool stays available (additive). No generic `/prompt run X` command — items stay `/<name>`. Covered by `test_prompt_invocation.py` (14). |
| **Skill-generation engine & library** | **wired + tested** | Spec'd in [ADR-0001](adr/0001-skill-engine-and-library.md) + [`skill-packaging.md`](skill-packaging.md). All parts ship: two skill kinds (typed `CASE`+`run` tools + `SKILL.md` playbooks), the `ack.skillgen` generator, the `ack.catalogue` library (semver + provenance), and the `ack.gate` registration gate. **`mpr` is the reference built-in** — now a **core always-on** built-in at `skills/mpr` (no `GX10_MPR` load gate; runtime `mpr.enabled`, default ON), consuming the core registry + `ack.i18n` + catalogue + gate; its `CASE` carries the catalogue manifest, indexed by `ack.catalogue`. Back-compatible (381 tests green, behavior unchanged when enabled; now part of the core `pytest` suite). The full lifecycle is verified end-to-end for both kinds (`test_skill_e2e.py`): generate → gate → register → load → invoke. |
| **Pluggable code-agent CLI** (`GX10_AGENT_CMD`) | **wired + tested** | The local code-agent the client launches per handover is a **command template**, not hard-wired to Claude Code — set `GX10_AGENT_CMD` to plug in any headless coding CLI with no code change (placeholders `{bin}/{model}/{effort}/{permission}/{prompt}/{feedback}`; `{permission}` defaults to `acceptEdits`, `{feedback}` is the optional result-capture path). **Claude Code and Codex (`codex exec`) are verified** backends; Codex drops `{effort}`/`{permission}` and uses `-o {feedback}` for the hybrid result capture. See [`code-agents.md`](code-agents.md). |
| **Config-driven code-agent registry** (`code_agents.pool`, #449) | **wired + tested** | Which agents EXIST (their identity, model + command shape) is a config-driven, always-on registry — `config.code_agents.pool`, a `providers.CodeAgentRegistry` keyed by `agent_id`, SEPARATE from the fan-out `providers.pool` (so it is independent of `providers.enabled`). Ironclad ships **OPUS**/**SONNET** as overridable defaults; `conf/` adds others (e.g. CODEX). `agent_id` is a letters-only filename token (round-trips both the handover + feedback filename regexes); an **unknown agent fails closed** at every gate (`stage_handover`/`advance_pipeline`, the autopilot reconciler, the server pull) — no silent default, no legacy alias. The handover `agent` schema enum is generated from the live registry. The **server resolves the full agent spec** (`bin`/`cmd_template`/`model`/`effort`/`permission`) into the `/pending` item; the client only renders it (one shared `build_agent_argv`). At boot the server **probes each enabled agent** (prompt-free; `bin` via PATH else the spec's private `bin_glob` newest-by-mtime) and is cli-available iff at least one resolves (#451). **`GET /coders`** (guarded) + the `/coders` client command show which coding agents are **bound** (registry + boot probe) plus the fan-out provider lane (`dispatcher.snapshot()`: pool reachability + last route reason + running budget); `/health` carries a compact `coders: {bound,total}` for the 2s poller (#452). During a fan-out the orchestrator emits a typed `[agent] <id> · <reason>` control frame per routed provider (the `[perf]` line-protocol pattern); every client parses it into the footer as a **live "which coder is being called" indicator** (#453). **`/coders use <id>`** pins which coding agent runs ALL handovers at runtime (`/coders use auto` clears it) — a `code_agents.pinned` override applied at every execution/reconciliation seam (`_effective_code_agent`), guarded `POST /coders`, fail-closed on an unknown agent; no pin ⇒ the orchestrator's task-chosen staged agent (#454). An out-of-budget agent is detected by a **server-side layered classifier** (JSON→stderr→exit, conf patterns; the client uploads the raw run signal) → a **process-lifetime circuit-breaker** trips and `_effective_code_agent` **fails over to the cheapest non-tripped capable peer**; `/coders` shows tripped agents, pinning one clears its breaker (#455). The failover is **task-class-scoped**: the class is derived deterministically from `task_json.type` (`gx10._task_class`; no model self-report trusted) and a `code_agents.classes` capability map restricts the peer search, so a budget-exhausted Opus on a **security**/**architecture** task never falls to a weaker agent (the staged pick + an operator pin stay authoritative; unmapped class ⇒ no restriction; all-capable-tripped ⇒ keep the chosen one) (#456). A code agent can be **onboarded while `enabled:false`** — inert (never offered/probed/launchable/failed-over-to) but shown in `/coders` as `(onboarded · disabled)` for operator visibility, the seam for a new backend pending exhausted-signal calibration (#460). See [`code-agents.md`](code-agents.md). |
| **Runtime contract self-check** (`GET /doctor`) | **wired + tested** | The same preflight the doctor CLI runs, exposed live so contract/registry drift surfaces at runtime (error/warn counts + a boot summary). Includes opt-in lodestar checks when enabled. |
| **Dev environment** (`Dockerfile.dev` + `docker-compose.dev.yml`) | **built (build+test gate)** | A reproducible, isolated dev setup that **builds the engine and runs the full suite inside a container** — the green build+test artifact is the precondition to promote. Resource-limited dev orchestrator, `open` profile, endpoint defaults to the reference stack and is overridable. Not needed to merely *use* Ironclad. See [`dev-environment.md`](dev-environment.md). |

## Memory

The engine has long-term memory **wired in**: a `query_memory` tool, store-on-task-
completion, and stage-time context injection, backed by `engine/memory.py` — a small,
secret-free client for a **Mem0-style HTTP service** (`POST /add`, `POST /search` with
`graph=false`, `GET /health`). Store + search are verified live against the reference
Mem0 stack (Qdrant + Neo4j + BGE-M3, LLM pointed at the local model).

**Three tiers (all server-side, fail-soft).** *Hot* = the model window (a bounded working
set, **token-accurately** trimmed against the model wall — real token counts from the served
model's tokenizer, with a calibrated chars/token fallback). *Warm* = `engine/warm.py`, a BSD-licensed in-memory store
(`GX10_WARM_URL`, e.g. Valkey) holding the rolling conversation summary + recent-turn state — which
**survive a restart** and are **shared across the reasoning workers** — plus a separate short-TTL
(~3 min) retrieval cache-aside. The shipped compose defaults `GX10_WARM_URL` to the `mem-valkey`
loopback, so the warm tier wires automatically under `--profile memory` (fail-soft otherwise). *Cold* = the Mem0 vector(+graph) store below. On top of the tiers:
rolling/hierarchical summarization on eviction (raw archived to cold first, then a **bounded
tail-first** summary input so the summarizer can't itself overflow the window, prefix-stable),
per-turn RAG assembly (warm cache-aside, the RAG block itself token-budgeted), token-accurate
budgeting (real token counts via the served model's tokenizer — the vLLM `/tokenize` endpoint —
with a calibrated chars/token fallback; the model window is auto-adopted from `/v1/models` at boot)
that scales the working set to the model window without overflowing it, a **pre-flight overflow guard** (before each call, reserve output —
`generation.max_tokens` / `GX10_MAX_TOKENS`, default 8192 — + the tools schema + the conditional
thinking budget; one emergency whole-round trim, then a clear `ContextOverflowError` instead of a raw
vLLM 400), chunked + recency-ranked long-artifact storage, an on-demand
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
  contract, the core is never *forked* — plugins dock through the stable contract and `core/` stays standalone + secret-free (boundary-check enforced; the check guards `core/`, it does not sandbox plugin code).
- **Mostly done — our internal release machinery.** The **DEV → Prod → Public promote** now
  runs as a **single fail-closed gated command**: boundary (core + client) → the full test suite
  (with a JUnit proof) → docs gate (CHANGELOG + doc-reality-audit) → export gates → a
  **PRE-publish clean-room** (wheel → fresh venv → import-smoke → an example plugin builds against
  the installed SDK) → a 2nd-instance review → publish → prod redeploy. It is **dry by default**
  and stops before anything irreversible. The remaining step is **full automation** (a scheduled
  evening sync). Downstream users never touch any of this — they pull the framework and extend it
  over the plugin API, or fork it freely (Apache-2.0).

So the way you *extend* Ironclad is shipped and tested; the way *we harden our own releases* is a
single gated flow, with scheduled automation the last step.

## Reference load test

Run against the full reference stack on the DGX Spark (2026-06-17): all five
containers up — `vllm-35b`, `mem-api`, `mem-qdrant`, `mem-neo4j`,
`ironclad-orchestrator` (all healthy) — driven from a workstation over the LAN.
*(The warm tier `mem-valkey` was added after this run — `--profile memory` now brings up **five**
containers: the four `mem-*` services + the always-on orchestrator; `vllm` is a separate
`--profile model`.)*

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
