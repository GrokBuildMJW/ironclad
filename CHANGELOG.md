# Changelog

All notable changes to Ironclad are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/); the project is **pre-release** (0.0.x).
Released versions are listed below; upcoming work accumulates under *Unreleased*.

> Docs are code: a change does not ship without a Changelog entry (the promote gate
> enforces a non-empty *Unreleased* section).

## [Unreleased]

## [0.0.20] - 2026-06-26

### Added
- **Proper `web_search` tool** (epic #505): the model runs web search natively from Ironclad with a
  strict input contract (`query`, optional `allowDomains`/`blockDomains` — mutually exclusive,
  normalized, wildcard-rejected), a **vendor-neutral adapter seam** (`cli` delegate · a native HTTP
  `brave` adapter on the standard library, **local-only** · a `mock` for tests), structured results
  with a measured duration, a **deterministic `Sources:`** block on every result, and a "web N · Xms"
  status-footer chip (stripped from the chat in every client). Outbound search is **blocked under the
  `sealed` trust profile** unless the operator opts in (`security.web_in_sealed`). Configured via the
  `search.*` block + `GX10_SEARCH_*` env; the API key is name-indirected from the environment, never
  config. `core/` stays standard-library-only (no new dependency). Robust against the model running a
  tool as a shell command (`execute_command` redirects a known tool name — e.g. `web_search "…"` — to
  the tool instead of a shell error), and the current-info steer covers news/headline phrasings. New
  `docs/web-search.md` + `docs/adr/0008-web-search-tool.md`.
- **`/health` reports the Warm tier separately (Cold ⇏ Warm)** (#385): `/health` reported a single
  `memory` field that reflected only **Cold** (Mem0), so a silent **Warm** (Valkey) outage — the tier is
  fail-soft and degrades to a no-op when unreachable — read as a healthy `memory: up` and could regress
  unnoticed. `/health` now also returns `warm` as `up` (reachable) / `down` (configured but unreachable) /
  `off` (not configured), and the Ink footer shows it next to `mem`. Also reconciles the docs that
  conflated Warm and Cold placement: **Cold (Mem0) is model-host-pinned** (GPU/LLM-coupled); the **Warm
  cache (Valkey) follows the orchestrator** — loopback is the ideal, a LAN hop to the model host is
  acceptable (Decision D-Valkey). `test_server_split.py` (+1), `clients/ink` (+warm footer assertions).
  (Core — `server.py` `/health`; client — Ink footer + status poller; docs — `setup-types.md`,
  `docker-compose.yml`.)

## [0.0.19] - 2026-06-25

### Changed
- **Node toolchain validated end-to-end (Node 22 → 24)** (#448, epic #440): the `clients/ink` CI now runs a
  **Node matrix `[22, 24]`** (`node-client.yml`), so CI validates BOTH the `engines` minimum (Node 22, npm 10)
  AND the version a desktop actually ships (Node 24, npm 11) — closing the gap where CI never tested the
  toolchain the operator builds/installs with. Each matrix leg uses its bundled npm, so the npm 10 ↔ 11
  resolution skew is covered; the `package.json` declares the canonical `packageManager` (`npm@11.17.0`) for
  corepack users (verified `npm ci` is in sync — no lockfile drift). A `process_doctor` invariant
  `node-version-matrix` keeps the matrix from regressing. The public `clean-room.yml` pre-publish job stays
  single-Node on purpose (it is a branch-protection required check; matrixing it would rename the check and
  needs a coordinated, operator-gated protection update). `test_process_doctor.py` (+2).

### Added
- **Read-only Memory MCP for external coding CLIs** (#480, epic #440 Phase 6 / FORK-G D2): a dependency-free
  **stdio MCP server** (`engine/memory_mcp.py`, JSON-RPC 2.0) that exposes the project memory to an
  MCP-capable code CLI (Codex/Claude) as **read-only** tools — `memory_search` (vector) and
  `memory_deep_query` (graph), no write (write-back deferred). The CLI spawns it as a subprocess; the
  code-agent registry injects the per-CLI MCP config via a new `{mcp}` multi-token placeholder in the
  `cmd_template` (filled from the agent's `mcp_template`). The injection is **gated server-side on the
  `sealed` trust profile** (operator) AND a configured memory service — under `open`/`token` the launch is
  byte-identical to before. **Secret-free**: the memory connection travels in the spawned process's env
  (`GX10_MEMORY_URL`/`GX10_MCP_MEMORY_NS`), never on the MCP wire; the read is scoped to the **project
  namespace** (the same memory the orchestrator + the #458 handover brief use), not the code-agent's id.
  Fully fail-soft (a memory hiccup returns a tool result, never crashes the server). `test_memory_mcp.py`
  (new, 10), `test_client_pool.py` (+1), `test_server_split.py` (+3). (Core — `memory_mcp.py` +
  `commands.build_agent_argv` `{mcp}` + `gx10`/`server` gate + `client` env; the per-CLI `mcp_template` is a
  private `conf/` detail.)
- **Onboard-but-disabled code agents** (#460, epic #440 Phase 6): a code agent can now be **registered
  while `enabled: false`** — onboarded but **inert** until activated. A disabled agent is excluded from the
  enabled-only launch/schema surface (`names`/`has`/`resolve`/`by_agent`), so it is never offered in the
  handover schema, never boot-probed, never launchable, and **never a budget-failover peer** (even if its id
  is listed in a `code_agents.classes` set — `resolve()` returns None, so it is skipped); `validate_loud`
  still checks it is well-formed. New `CodeAgentRegistry.all_ids()`/`spec_of()` expose ALL registered agents
  (including disabled) for **operator visibility**: `GET /coders` (and all four clients) now show an
  onboarded-but-disabled agent as `enabled:false` / `(onboarded · disabled)`, so the operator can see a
  registered agent that is pending activation. This is the onboarding seam for a new backend whose
  exhausted-signal is calibrated from one real run before it is enabled. `test_code_agent_registry.py` (+3),
  `test_server_split.py` (+1). (Core — `providers.py`/`server.py` + the four clients.)
- **First-class web search + current-info routing + shell guardrail** (#459, epic #440 Phase 6 / §4 /
  FORK-H — fixes the verified scaling-break #447): the orchestrator gains a real `web_search` tool so the
  model never has to improvise a shell web fetch for current information. Three parts: **(1)** a `web_search`
  tool, offered only when a **web-capable provider is configured**, that runs the search **server-side
  through the provider lane** (`ProviderDispatcher.web_search` routes a `needs_web`/PUBLIC request to the
  web provider and runs it via the **captured** CLI runner — structurally immune to the console write that
  corrupted the renderer); **(2)** a conservative EN+DE **current-info intent classifier** that proactively
  steers "latest / today / aktuelle Lage" requests to `web_search`; **(3)** a **fail-closed shell
  guardrail** on `execute_command` — a remote/web fetch (`Invoke-WebRequest`, `curl`, …) or an
  unbounded/progress-emitting process (sleep loops, `-Wait` follows, watchers, `ping -t`) is **refused
  before it runs** (and a web fetch is redirected to `web_search`), plus the PowerShell invocation is
  hardened with `$ProgressPreference='SilentlyContinue'`. The guard fires **server-side at the top of
  `run_tool`, before the local-tool bridge**, so it covers every client (the thin client AND the Ink
  client, which also gets the PowerShell hardening at its own execution site). The deny-list anchors bare
  `curl`/`wget` to a command position (a filename/search string merely *containing* the token isn't
  blocked), and the intent classifier avoids bare "current" (everywhere in coding context). The web CLI
  itself (e.g. Codex `--search`) is a private `conf/` deployment detail; core ships the capability-gated
  mechanism. The guard is **mirrored into the Ink client** (`shellGuard` in `runTool.ts`) so the local
  `/sh` escape hatch — which never reaches the server — is also covered, and the web provider must be an
  **enabled external CLI** (an in-engine/disabled web spec is never offered or routed). `test_dispatch.py`
  (+7), `test_websearch.py` (+46), `clients/ink` `runTool.test.ts` (+4). (Core — `dispatch.py`/`gx10.py`;
  client — `clients/ink/src/tools/runTool.ts`.)
- **Token-budgeted Memory brief in the handover** (#458, epic #440 Phase 6 / FORK-G D1): the plain
  `type: title` `get_context` that was appended to a staged handover is replaced by a richer, token-budgeted
  **Memory brief** for every code agent — so the external coding CLI starts a task with the same memory the
  orchestrator has. The brief composes, in priority order and trimmed to stay within a token budget
  (`context.memory_brief_tokens`, default 1200; counted with the best-available tokenizer): (1) the shared
  **warm rolling summary** (the main loop's common ground), (2) **body-keyed** vector hits — the handover
  BODY is a far richer retrieval query than `type: title`, (3) optional **relational** (graph) hits, deduped
  against the vector hits. Fully **fail-soft**: any memory hiccup (or nothing relevant) just stages the
  handover without a brief, and a vector-search error still keeps a warm summary already in hand. `get_context`
  remains for plain callers. `test_memory.py` (+7). (Core — `memory.py` `MemoryManager.brief` + the
  `stage_handover` injection in `gx10.py`.) The read-only Memory **MCP** for external CLIs (D2) is tracked
  separately (#480, stdio transport).
- **Distinct-reviewer routing (anti-affinity)** (#457, epic #440 Phase 5): the pure provider router can be
  told to **avoid the agent that produced the artifact under review**, so a review-of-a-review is never
  routed back to its own author while an equal peer is available. A new `RouteRequest.excluded_provider_ids`
  (caller-passed — `route_one` stays pure and snapshot-testable) drops those providers from the candidate
  set after the capability filter, before load/spill/budget (so the producer can't slip back in via a
  fallback). It is **SOFT**: if excluding the producer would leave no capable agent, the route is not
  declined — the producer is kept and the decision records the waive. `RouteDecision.distinct_reviewer`
  carries the provenance: `"applied"` (an excluded producer was dropped and an equal peer chosen),
  `"waived"` (the producer was the only capable agent), or `None` (no exclusion requested). **HARD axes
  outrank it**: a SENSITIVE/local-only request whose only local provider is the excluded producer stays
  local (waived) rather than leaking to an external "distinct" peer. `test_router.py` (+5). (Pure router
  seam — `router.py`; a private CI invariant `review-distinct-reviewer` asserts the seam stays intact.)
- **Task-class-scoped budget failover** (#456, epic #440 Phase 5): the #455 failover now stays within the
  agents that are *capable of the task's class*, so a budget-exhausted Opus on a **security** or
  **architecture** task never silently falls to a cheaper-but-weaker peer. The task class is derived
  **deterministically from `task_json.type`** (`gx10._task_class`, FORK-D — no model self-report is
  trusted): `security`/`security-audit` → `security`, `architecture` → `architecture`, `verification`
  → `analysis`, everything else → `coding`. A new `code_agents.classes` capability map
  (`security: [OPUS]`, `architecture: [OPUS]`, `coding: [OPUS, SONNET]`, `analysis: [SONNET]`) names which
  registry agents may serve each class; `_effective_code_agent(staged, task_class=…)` restricts the
  cheapest-non-tripped-peer search to that set. The **staged (orchestrator-chosen) agent stays
  authoritative** and an operator pin still wins — the class only *scopes the failover peers*. Fail-open
  by design: an unknown/unmapped class (or no class) imposes no restriction, byte-identical to #455; if
  every capable agent is tripped the chosen one is kept (fail-closed, never an out-of-class agent).
  `test_server_split.py` (+12). (Core seam — `gx10.py`/`server.py`; the conf `classes` map refines the
  per-deployment roster privately.)
- **Budget-exhausted classifier + circuit-breaker + equal-peer failover** (#455, epic #440 Phase 4):
  turns an out-of-budget agent's silent infinite-retry into a clean failover. The client now reports the
  raw run signal (exit code + a bounded stderr tail) to the server, which **classifies** it
  (`providers.classify_agent_result`, FORK-C=C: layered JSON-event → stderr regex → exit code, patterns
  from conf) into `ok-feedback` / `task-failed` / `agent-unavailable`. A run that produced FEEDBACK is
  always `ok-feedback` — the feedback (the agent's task result) is never pattern-matched (so a coding
  answer that mentions "rate limit"/"quota" can't false-trip); only the RAW stderr is scanned, only when
  there is no feedback. **Conservative**: only an explicit exhausted match is `agent-unavailable` — an
  unknown failure is `task-failed`, never a wasteful failover. On `agent-unavailable` the server trips a **process-lifetime circuit-breaker**, and
  `_effective_code_agent` fails over to the **cheapest non-tripped capable peer** (USD soft ordering;
  all tripped → keep the chosen one, fail-closed). `GET /coders` shows breaker-tripped agents; pinning an
  agent (`/coders use <id>`) clears its breaker (recovery). Generic, public-safe exhausted patterns ship
  in core; a deployment refines per-agent in `conf/` — an agent's exact signal is calibrated from one
  consented run (e.g. Kimi at #460). `test_code_agent_registry.py` (+9), `test_server_split.py` (+4).
  (Core seam — `providers.py`/`gx10.py`/`server.py`/`client.py`; conf patterns private.)
- **Runtime coding-agent switching** (#454, epic #440 Phase 4): the operator can pin which coding
  agent handles ALL handovers at runtime with `/coders use <id>` (and `/coders use auto` to clear) —
  the runtime "switch" between equal-strength agents. A new `code_agents.pinned` runtime config, a
  guarded `POST /coders` (validates the agent against the registry, fail-closed on unknown), and a
  single `gx10._effective_code_agent(staged)` seam that **overrides the orchestrator's task-chosen
  (staged) agent** at every execution/reconciliation point (`/pending` spec resolution, the reconciler
  feedback match — with a staged fallback if the pin changes mid-handover, and the autopilot launch).
  No pin ⇒ the staged agent (task-appropriate — the orchestrator already routes "Opus for
  security/architecture"); cost-based auto-routing among task-equal peers is deferred to the Phase 5
  `task_class` work. `GET /coders` + the `/coders` view (all four clients) show the active pin.
  `test_server_split.py` (+5). (Core seam — `gx10.py`/`server.py` + the four clients.)
- **Live "which coder is being called" indicator** (#453, epic #440 Phase 4): the fan-out routing
  provenance (`provider_id`/`route_reason`/`spilled`) is now surfaced as a typed, backward-compatible
  `[agent]` control frame — the same line-protocol pattern as `[perf]`. The orchestrator emits one
  frame per distinct routed provider (`_emit_agent_frames`, fail-soft); every client parses it out of
  the chat stream into the status footer (`coder <id> · <reason>`): the TypeScript client
  (`stream/route.ts` + `Footer`), the Rich full-screen client, the Textual client, and the REPL. The
  parser is a byte-exact port across the Python (`cli.py`) and TypeScript renderers. `test_server_split.py`
  (+2), `route.test.ts`/`components.test.tsx` (+1). (Core seam — `gx10.py` + the four clients.)
- **`/coders` — which coding agents are bound + active** (#452, epic #440 Phase 4): a new guarded
  `GET /coders` and a `/coders` client command (REPL, full-screen, and the TypeScript client) answer
  "which coding agents are actually bound right now". It surfaces the config-driven code-agent registry
  with each agent's prompt-free **boot-probe liveness** (bin resolved = bound) alongside the fan-out
  provider lane via a new `ProviderDispatcher.snapshot()` (per-provider reachability + last routing
  reason + running budget). `/health` carries a compact `coders: {bound,total}` for the 2s poller.
  `GET /coders` is gated like `/tasks`/`/doctor`. `test_dispatch.py` (+4), `test_server_split.py` (+4),
  `classify.test.ts` (+1). (Core seam — `dispatch.py`/`server.py`/`commands.py`/`client.py`/`cli.py` +
  `clients/ink`.)
- **Per-agent boot probe** (#451, epic #440 Phase 3): the server checked a single `which(CLAUDE_BIN)` to
  decide whether a local code-agent was available. It now probes EACH enabled code-agent (prompt-free
  path resolution, `providers.probe_code_agents`) and is cli-available iff AT LEAST ONE resolves —
  fail-closed only when ZERO do. Each agent's binary resolves via `PATH` (a stable shim) else the spec's
  private-layer `bin_glob` newest-by-mtime (FORK-A3: the hashed AppData launcher path rots on update);
  env vars/`~` in `bin_glob` are expanded, and the concrete path stays in `conf/` (never a literal in
  `core/`). Boot logs each agent's resolution. `test_code_agent_registry.py` (+7). (Core seam —
  `providers.py`/`server.py`; private config — `conf/`.)
- **Config-driven code-agent registry — the multi-CLI spine** (#449, epic #440 Phase 3): the handover
  code-agent identity was fused to Claude across six OPUS/SONNET allowlists, a `client._MODEL_BY_AGENT`
  table and a legacy KIMI→SONNET normalization. Agents now live in ONE config-driven, always-on surface —
  `config.code_agents.pool`, a `providers.CodeAgentRegistry` keyed by `agent_id` — SEPARATE from the
  fan-out `providers.pool` (so it is independent of `providers.enabled`, which is on in local-mode).
  Ironclad ships **OPUS**/**SONNET** as **overridable** defaults; `conf/` adds CODEX (KIMI at #460). The
  `agent_id` is a letters-only filename token (it must round-trip BOTH `_HO_AGENT_RE` and `_FB_RE`); an
  **unknown agent fails closed** everywhere (the two `stage_handover`/`advance_pipeline` guards, the
  autopilot reconciler, the server pull) instead of silently defaulting. The handover schema `agent` enum
  is generated from the LIVE registry, so a conf-added agent is offerable. The server now resolves the
  FULL agent spec (`bin`/`cmd_template`/`model`/`effort`/`permission`) from the registry into the
  `/pending` item and the client is a thin renderer (no client-side registry); the handover-frontmatter
  `to:`/`effort:` still override. Both launch paths share one `build_agent_argv` (moved to stdlib
  `commands.py` so the zero-dependency client never pulls pydantic). `test_code_agent_registry.py` (+32).
  (Core seam — `providers.py`/`gx10.py`/`server.py`/`client.py`/`commands.py`; private config — `conf/`.)
- **Deterministic code-agent result capture (hybrid feedback)** (#443, epic #440 Phase 2): the handover
  runner read only the agent-written `…-feedback.md`, so an agent that finished its work but forgot to
  write that file produced a silent no-feedback → retry. `_build_agent_argv` now accepts an optional
  `{feedback}` token (a result-capture path) and `_run_handover` threads it through; an agent whose
  template uses it — e.g. Codex `-o {feedback}` (now in the conf Codex entry) — writes its FINAL message
  there, and the runner falls back to that captured message when the in-prompt feedback file is missing
  (FORK-A2 = C, hybrid: file is primary, capture is the fallback). Claude's default template omits
  `{feedback}` and is unchanged. `test_client_pool.py` (+4). (Core seam — `client.py`; private conf.)
- **Codex is a first-class code-agent backend on the template-driven client lane** (#442, epic #440 Phase 2):
  a `codex` provider entry in the private `conf/` pool proves the existing template-driven handover lane
  runs Codex with ZERO core change. The Codex `cmd_template` (`codex exec -m {model} -s workspace-write -c
  'approval_policy="never"' --skip-git-repo-check {prompt}`) **drops `{effort}`/`{permission}`** — `codex
  exec` rejects `--effort`/`--permission-mode`/`-a` (verified live, §C0R-8) — and `_build_agent_argv` leaks
  none of the Claude-only flags/values. `bin: "codex"` is the logical name; the per-provider boot probe
  (#451) resolves it glob-newest from `%LOCALAPPDATA%\OpenAI\Codex\bin` (the hashed path rots). Output
  capture (`-o`/`--json`) is #443; the live workspace-write run is operator-verified later.
  `test_client_pool.py` (+1). (Private config — `conf/`; the BYO-code-agent guide
  `code-agents.md` + `status.md` gain a verified Codex example + the `{feedback}` placeholder, #444.)
- **Multi-line paste collapses to a `[Pasted #N +L lines]` placeholder in the TUI input** (#438): pasting
  more than one line into the chat input now shows a compact placeholder (like Claude Code) instead of the
  raw lines, and expands back to the full text when the turn is submitted — so a large paste no longer
  floods the input line. Re-introduces the model the retired Python TUI had (`tui.py`), ported to the
  TypeScript client: bracketed **and right-click** pastes are flagged (`key.paste`) at the mount layer, a
  pure `pasteStore` module owns the compress/expand/Backspace logic, and `App.tsx` keeps the per-turn
  block store. Hardened after an adversarial review: the buffer holds an **out-of-band sentinel** (not the
  visible grammar), so typing or single-line-pasting the literal `[Pasted #N +L lines]` is never expanded
  or over-deleted; deleting a collapsed paste **reclaims** its stored block; the sentinel delimiters are
  stripped from incoming paste content so a paste can't forge one; and LF/CRLF/lone-CR are all treated as
  line breaks. Single-line pastes and typed input are unaffected. `node:test` (+13).

## [0.0.18] - 2026-06-24

### Fixed
- **Token-accurate context budgeting** (#371, epic #366 P1 1/3): the context budget was char-based
  (`CHARS_PER_TOKEN = 4`) against a hard 32 768-**token** wall. Real agent content (code/JSON/CJK) is
  ~2–2.6 chars/token, so the working set silently exceeded the window and vLLM rejected the request
  before generating (`HTTP 400 "maximum context length is 32768"`, `0 gen · 0 tok`). The trim now
  counts **real tokens** via the served model's tokenizer (the vLLM `/tokenize` endpoint — no bundled
  tokenizer dependency) and the RAG block is budgeted in real tokens; the calibrated chars/token
  fallback (default **2.6**) is used only when the endpoint is unreachable (conservative — it trims
  early rather than overflowing). Fail-soft: a tokenizer outage never makes the engine 400. Also fixes
  the false `_derive_ctx_budget` docstring ("never overflows it" held only at 4 c/t). See ADR-0003.
- **Pre-flight overflow guard + emergency single-turn trim** (#372, epic #366 P1 2/3): `_make_completion`
  now checks, before every vLLM call, that the prompt + the reserves it must leave free — output
  (`max_tokens`) + the **tools schema** vLLM serializes into the prompt + the **conditional thinking
  budget** (only when `think=True`) — fit the model window. If not, it does ONE emergency trim of the
  oldest **whole** rounds (atomic `assistant.tool_calls` + their `tool` responses, else a different 400);
  if it still can't fit (an irreducible single oversized turn) it raises a clear **`ContextOverflowError`**
  (prompt/output/window sizes) instead of a raw vLLM 400. Evicted rounds are archived losslessly to cold.
  Fail-soft (skipped when token budgeting is off). See ADR-0003.
- **Bounded summarizer input** (#373, epic #366 P1 3/3): `_summarize` capped only its OUTPUT
  (`SUMMARY_MAX_TOKENS`), never its INPUT — a large evicted transcript was fed whole, so the summarizer
  call itself could hit the model window and vLLM would silently truncate it (a lossy rolling summary,
  state loss over long sessions). The input is now bounded token-based, **tail-first**
  (`input_budget = min(4096, max_model_len // 4)`); the **full raw transcript is still archived losslessly
  to cold first**, then only the most-recent (tail) rounds within budget are summarized (a warning is
  logged on truncation). Completes the P1 trio. See ADR-0003.
- **`ironclad` `/exit` now stops the local background engine** (#428): the launcher
  (`core/install/ironclad.ps1`) started or reused a local `server.py` and only stopped it on exit
  *when this session had started it* (`$started`), so a **reused or orphaned engine lingered on the
  port** after `/exit` (the CLI quit but `server.py` kept serving). The teardown now stops the local
  engine by its listening port regardless — the engine is ephemeral per session (single-tenant by
  design; the `spark` thin-client path is unaffected — it has no local engine).

### Added
- **`GX10_CHARS_PER_TOKEN`** / **`GX10_TOKENIZE`** env knobs (#371): tune the calibrated fallback
  ratio; `GX10_TOKENIZE` is `auto` (probe a real remote/LAN host), `1` (force — server-mode loopback),
  or `0` (pure char fallback).
- **`GX10_THINKING_RESERVE`** env knob (#372, default 4000): output headroom reserved for the thinking
  budget, applied only when thinking is on for the call.
- **Live `max_model_len` discovery** (#377, epic #366 P2): the engine reads the served model window
  from `GET /v1/models` at boot and adopts it (re-deriving the char-fallback watermarks), so the token
  budget can't drift if the Spark is relaunched with a different `--max-model-len`. Fail-soft (keeps the
  configured `MAX_MODEL_LEN` on any error); only a real remote/LAN host (the offline suite stays
  hermetic); `GX10_DISCOVER_WINDOW=0` disables.

### Documentation
- **Windows conhost copy/scaling limitation** (#382, epic #366 P3): documented the legacy Windows
  console (`conhost.exe`) right-click-copy / scaling limitation in the `clients/ink` docs — the renderer
  owns selection (alt-buffer + SGR mouse tracking), so on conhost a right-click copy is captured by the
  app; *workaround:* Windows Terminal + Shift-drag. Terminal limitation, not a renderer bug; shares the
  alt-buffer + mouse-tracking class with #256 (where the client-side fix is tracked). Docs only.
- **Output reserve is tunable** (#379, epic #366 P2): documented that the output (generation) token
  reserve — `generation.max_tokens` / **`GX10_MAX_TOKENS`**, default **8192** — is the permanent output
  budget subtracted from the model window. The default stays 8192 (raising it from 4096 in PERF-10 fixed
  long-handover truncation; post-#371 it is reserved token-accurately). Raise it for longer single
  outputs, lower it for more context headroom. Addresses C-5.

## [0.0.17] - 2026-06-24

### Fixed
- **Publish workflow permissions** (#411): the publish job set `permissions: id-token: write` explicitly,
  which drops the default `contents: read`, so `actions/checkout` failed on a private repo
  (*"repository not found"*). Grant `contents: read` back (a no-op on the public repo; required for a
  private Test-PyPI proof repo).
- **Post-publish-smoke install index** (#413): the post-publish smoke `pip install`ed `ironclad-ai==<ver>`
  with no index, so it queried production PyPI even for a Test-PyPI release and could not find the version.
  Install from the SAME index the release published to (derive the simple index from `PYPI_REPOSITORY_URL`
  + add production as an `--extra-index-url` for dependencies); unset keeps production-default.

### Added
- **Repo-scoped publish index** (#397, epic #348 S14c): the publish workflow's `repository-url` is now
  repo-scoped via the `PYPI_REPOSITORY_URL` variable (default: production PyPI). This lets a separate
  Test-PyPI proof repo (`ironclad-testpypi`, with its own Test-PyPI Trusted Publisher + the variable set to
  `https://test.pypi.org/legacy/`) publish the SAME generated workflow to Test-PyPI — so the release chain
  is proven on Test-PyPI first, with both push- and index-isolation, before any production cut. Production
  `ironclad` is unaffected (the variable is unset → production PyPI).
- **Deploy/spark consistency** (#216, epic #210): the deployed Spark topology is a derived view of the
  released artifact. CI-runnable (no SSH): `scripts/ci/check_deploy_consistency.py` asserts every literal
  `setup.type` value in the deploy scripts is one the engine accepts (`_VALID_SETUP_TYPES`) and that no
  script references a missing `deploy/…` script — caught + fixed a terminology drift (two scripts said
  `setup.type=desktop`, an install-type name the engine rejects). Operator (SSH + local): `deploy/spark/
  verify-deployment.sh [--reconcile]` checks the live deployment — **topology- AND location-aware**: the
  model is probed on the Spark, and the orchestrator is verified **where it actually runs** — on the
  **desktop** (`localhost:8100`) for `setup.type=local`, on the Spark for `server` — asserting `/health`
  `ok`, `memory=up`, and (local) that `base_url` points at the Spark over the LAN. Verified live: the
  current `setup.type=local` deployment is consistent (Spark model up; desktop orchestrator up, memory up,
  wired to the Spark). `test_deploy_consistency.py` (5). Per ADR-0007.
- **Label taxonomy + anchor-hygiene invariants** (#215, epic #210): three warn-tier process-doctor
  checks surfacing governance drift for triage — `labels-match-taxonomy` (the repo's defined labels
  equal the bootstrap taxonomy: a rogue or a deleted label), `issue-has-type-label` (every open issue
  carries exactly one `type/*`), and `merged-pr-anchored` (every merged PR links an issue, release/
  export PRs excluded). `test_process_doctor.py` (+5). Per ADR-0007.
- **Required-status-checks SSOT + audit** (#214, epic #210): the branch-protection required checks were
  invisible to version control, and the #196 `secret-scan` job was **not** required — a secret leak
  wouldn't block a merge. Added `.github/required-status-checks.yml` (SSOT) + `check_required_checks.py`
  (asserts every required name maps to a real workflow job, matrix names expanded — CI-gated, no API) +
  process-doctor `required-checks-live` (asserts the live protection equals the SSOT; fail-soft without
  an admin read). One-shot: added `secret-scan` to the live ironclad required checks. `test_required_checks.py`
  (5) + `test_process_doctor.py` (+2). Per ADR-0007.
- **Plugin-mirror parity** (#213, epic #210): the plugin round-trip (`mirror-from-plugin.yml`) lacked
  the back-link + liveness invariants the upstream one got, and labelled `triaged` BEFORE creating the
  dev mirror (a partial failure stranded a framework-gap report). Hardened the workflow to
  create-before-triage (parity with #194) and added two scheduled process-doctor warn checks —
  `plugin-triaged-has-mirror` (a triaged plugin issue with no dev mirror) + `plugin-mirror-live` (the
  intake's last run failed). Both fail-soft when the operator-gated plugin repo is unreachable.
  `test_process_doctor.py` (+3 = 29). Per ADR-0007.
- **Release tag ↔ CHANGELOG ↔ pyproject coupling** (#212, epic #210): `release_preflight` (#198) only
  guards PRE-publish; a post-release metadata mutation (a deleted tag, a reverted CHANGELOG section)
  is seen by no path-gated CI. Two scheduled process-doctor checks close it: `release-tag-has-changelog`
  (every published ironclad release tag has a `## [X.Y.Z]` section — fail) and `changelog-has-release-tag`
  (every released section has a tag — warn, excluding the cut-but-unreleased current version). Healed the
  drift it found: backfilled the missing `[0.0.1]` + `[0.0.2]` sections (early pre-releases predating the
  CHANGELOG). `test_process_doctor.py` (+4). Per ADR-0007.
- **Test counts are generated, not hand-maintained** (#211, epic #210): the Python test counts in
  README + `docs/test-report.md` are a derived view of the suite (they drifted every PR). New
  `scripts/ci/gen_test_counts.py` runs the offline suite and `--check` fails on any drift / `--write`
  regenerates; it also asserts the per-area breakdown is a true partition (the area rows sum to the
  total) and that every offline skip is a live-smoke test (so the offline/live split is honest). A CI
  `test-counts` job enforces it. `test_gen_test_counts.py` (9). Per ADR-0007.

### Fixed
- **ironclad-doctor reports the running engine version, not just the install stamp** (#255): on a
  desktop install the doctor printed `local engine version` straight from the on-disk `VERSION` file
  (the *installed* stamp), so an installed-vs-running drift was invisible — `ironclad-install` re-stamps
  and re-copies `core/` but does not restart the live engine (the `ironclad` launcher does, on next
  start), and `orchestrator_version` is frozen at boot. The doctor now also reads the running engine's
  `/health.orchestrator_version` and prints `installed engine version=X` plus the running version,
  warning `running 'Y' != installed 'X' — run 'ironclad' to restart` on a mismatch.
- **Example plugin passes its own documented `ack.sdk.gate`** (#260): `examples/example-plugin` shipped
  no sibling test, so the validate step it documents (`gate("…/reverse.py")`) failed out of the box —
  the gate hard-requires `<package>/tests/test_<stem>.py` (`ack/gate.py`). It now ships
  `ironclad_example_plugin/tests/test_reverse.py`; the README shows the gate as a passing `assert`, and
  `test_example_plugin.py` asserts the gate passes (regression guard). Surfaced via the plugin round-trip.

## [0.0.15] - 2026-06-22

### Added
- **Release-version invariant + release-aware CHANGELOG gate** (#198, folds #177, epic #188): a release
  is an irreversible publish (ADR-0007). `publish.yml` now runs a **fail-closed preflight before the
  PyPI upload** — the release tag, `pyproject.version` and a non-empty `## [X.Y.Z]` CHANGELOG section
  must all agree (a duplicate version is backstopped by the non-skip upload). `release-close.yml` is
  **gated on publish success** (triggered by the Publish workflow completing `success`) so a reporter's
  issue is never closed "released and available now" before the package is actually live. New
  `scripts/ci/release_preflight.py` carries the pure logic (`release_preflight`, `changelog_release_state`)
  and a `--preflight` CLI that also checks PyPI. **Fixes #177:** promote.sh gate 3 demanded a non-empty
  `[Unreleased]`, which was mutually exclusive with the post-bump state doc-reality-audit demands
  (`pyproject == newest CHANGELOG`); the gate now classifies pending-dev and cut-release as valid and
  only genuine drift as fail, so the whole release flow runs. `test_release_preflight.py` (9).
- **Upstream round-trip + board reconcilers** (#194, epic #188): four invariants on the cross-repo
  and board derived views (ADR-0007). `upstream-closed-is-released` — a public `ironclad` issue that
  reached `resolved` may only be closed via delivery (which stamps `released`); a closed-without-
  `released` issue is drift and is healed (fixed the stranded `ironclad#5`). `open-assigned-in-progress`
  — an open + **assigned** issue must be at least In Progress on the board, healed by adding it and
  setting the column (closes the gap that left the *active epic* invisible while its sub-issues moved).
  `upstream-triaged-has-mirror` + `mirror-wiring-live` (warn) detect a stranded triage and a rotted
  intake. `mirror-from-public.yml` is hardened to **create the mirror before** labelling `triaged`
  (a partial failure can no longer strand a report), and `reconcile.yml` routes the upstream heals via
  `UPSTREAM_TOKEN` and board heals via `PROJECTS_TOKEN`. `test_process_doctor.py` (+7). 
- **Secret scan un-degraded** (#196, epic #188): the export secret-gate could pass **degraded** when
  gitleaks was absent (fail-open). `export_core.py --require-scanner` (+ `EXPORT_REQUIRE_SCANNER=1`)
  now **fail-closes** if no scanner ran; a new private CI `secret-scan` job installs gitleaks and
  exports with that flag, and the **public** `ironclad` CI gains a `gitleaks` job so a secret pushed
  straight to the public repo is caught too (the private gate can't see a public hand-push).
  `test_export_secret_gate.py` (2). Per ADR-0007.
- **Export↔public byte-equality check** (#195, epic #188): `scripts/ci/export_sync_check.py` +
  `export-sync-check.yml` (scheduled + on core/ push) assert the public `ironclad` repo is a faithful
  export of `core/` — LF-normalised (a CRLF-only diff is never drift). A file present in public that
  the export does not produce (a hand-edit) is always drift; when public's `.export-source` stamp ==
  HEAD the trees must be byte-identical. `publish_core.sh` now writes that source stamp on push so
  drift is distinguishable from the normal "main ahead of the published release" state.
  `test_export_sync_check.py` (5). Per ADR-0007.
- **DEV_LOOP self-consistency lint** (#197, epic #188): the binding control prompt is itself a
  derived view — process-doctor `devloop-self-consistent` asserts every `*.yml` it cites resolves to
  a real file and every `aktuell v<X>` equals the pyproject version. Fixed the live drift it caught:
  the ghost `mirror-to-dev.yml` ref (real one is `mirror-from-public.yml`), the stale `v0.0.7`
  pointers (→ v0.0.14), and the drifty hard-coded test counts (→ point to test-report.md).
  `test_process_doctor.py` (+3 = 15). Per ADR-0007.
- **Doc-lint coverage** (#193, epic #188): a process-doctor warn `open-milestone-has-description`
  (an open milestone with work but no description is invisible on the generated roadmap); the release
  gate (`promote.sh`) now runs `gen_roadmap.py --check` so a release can never ship a stale roadmap.
  docs-guide records the enforcement. `test_process_doctor.py` (+1 = 12). Per ADR-0007.
- **Issue/milestone invariants in process-doctor** (#192, epic #188): `delivered-milestone-closed`
  (a milestone with work done + 0 open issues must be closed so the generated roadmap drops it —
  assert + heal) and `open-epic-has-milestone` (an open `type/feature` epic with no milestone is
  invisible to the roadmap — **warn**, surfaced for operator triage; picking the milestone is a
  product call). Adds a `warn` tier to the check framework. `test_process_doctor.py` (+3 = 11).
  (Live: #75 surfaced for milestone triage.) Per ADR-0007.
- **Board invariant: closed issue ⇒ board Done** (#191, epic #188): the Projects-board card race
  (#176) is now structurally closed. `project-status.yml` gains a **closed-guard** that re-queries
  **live** issue state (not the event payload) and refuses to set In Progress on a closed issue;
  process-doctor gains a `board-closed-is-done` check (assert + heal) so the **scheduled** reconciler
  is the load-bearing healer that returns any stuck card to Done regardless of Action-run ordering.
  `test_process_doctor.py` (+3 = 8). Per ADR-0007.
- **process-doctor + scheduled reconciler backbone** (#190, epic #188): `scripts/ci/process_doctor.py`
  — an executable check registry that asserts the derived-view invariants against live GitHub state
  (`--check`) and heals them idempotently (`--reconcile`), fail-closed (a gh **auth** failure
  hard-fails, never soft-skips). Seed invariant: *a closed issue carries no `status/*` label* (assert
  + heal; the 12 live stale labels were reconciled). A scheduled `.github/workflows/reconcile.yml`
  runs the healer daily — the load-bearing leg that catches metadata-only mutations (Gap C: a
  milestone/label change triggers no path-gated push check). `gen_roadmap.py --check` now hard-fails
  on a gh auth failure (distinct from a network soft-skip). `test_process_doctor.py` (5). Per ADR-0007.
- **ADR-0007 — reconcilers + invariants over every derived view** (#189, epic #188): generalises
  ADR-0006 from the roadmap to the whole "derived view drifts from reality" class (board, issue/label/
  milestone metadata, mirror/upstream, public export, secret gate, DEV_LOOP). Each view gets an
  invariant + on-event guard (re-queried live state) + a **scheduled reconciler** (the load-bearing
  healer; idempotent, fail-closed), all asserted by an executable `process-doctor` with a negative
  test per invariant. Records the Wave-1 scope + the deferred follow-ups (deep CI required-checks,
  deploy/spark prod).

## [0.0.14] - 2026-06-22

### Changed
- **Roadmap rule: open-milestone (not open-epic)** (#185, epic #169): the generated roadmap now
  renders one section per **open milestone** (its description = the phase narrative); a delivered
  phase drops by **closing its milestone** (M3 + M6 closed). This refines #176's initial
  "milestone with ≥1 open epic" rule, which would have erased an active phase the moment its current
  epics merged (e.g. M5 vanishing when epic #169 itself closed). ADR-0006 D2/D3, `docs-guide.md`,
  and the DEV_LOOP/NEW_EPIC C2 prune wording updated to match.

### Added
- **Generated, drift-proof roadmap** (#176, epic #169): `roadmap.md` is now produced by
  `scripts/ci/gen_roadmap.py` from the **open roadmap phases** (open milestones with ≥1 open
  `type/feature` epic) — a phase drops off automatically once its epics close, so realized work can
  never linger (the structural fix). The per-phase narrative lives in the **milestone description**
  (the single editable source); the generator only renders. A CI job (`roadmap-generated`)
  regenerates and fails on drift (soft-skips if the GitHub API is unavailable; the offline per-doc
  lint is the always-on guard). Pure `render_roadmap` is unit-tested; `test_gen_roadmap.py` (5).
  Per ADR-0006.
- **doc-reality-audit: per-doc responsibility lint** (#174, epic #169): a new fail-closed check —
  `roadmap.md` must contain no realized markers (`shipped`/`delivered`/`wired + tested`/`now|generally
  available`) and `status.md` no future markers (`coming soon`/`will ship`). File-scoped + tiny
  marker lists so legitimate usage (status.md's own `wired + tested`, `see the roadmap` pointers) is
  never flagged. Covered by `test_doc_audit.py` (9) incl. the **negative test** (a deliberately
  "realized" roadmap item makes the audit FAIL) + a real-docs-pass regression. Per ADR-0006.

### Changed
- **README reconciled to the doc IA** (#173, epic #169): the `## Roadmap` section is now a clean
  pointer (planned → roadmap.md, runs-now → status.md) instead of a duplicated, drift-prone
  "done vs planned" list — fixing the stale claim that roadmap.md shows "what works today" and the
  outdated "Phase g" reference. README stays intro/quickstart/value-prop; the per-component wiring
  matrix lives only in status.md (no overlap). (Repo hygiene: the untracked root-level duplicate of
  `vault/Plan/plan_skill_libary.md` — a byte-identical local stray — was removed; the tracked copy
  is the canonical one.)
- **roadmap.md is now future-only** (#172, epic #169): removed the realized content that violated the
  "forward-looking only" contract — the delivered skill-generation engine (ADR-0001/0002), the
  prompt library + discovery/invocation (ADR-0003/0005), and the shipped Extension SDK (ADR-0004),
  plus the two fully-closed phases (skill-generation, prompt-library usability). That content lives in
  `status.md` (wiring SSOT) + `CHANGELOG` history. Roadmap now carries only open themes (enterprise,
  connectors, broader model/data, release maturity). Deferred-but-unscoped items (skill/prompt
  curation, save-as-item) have no open epic yet → tracked in [ADR-0005](docs/adr/0005-prompt-skill-discovery-invocation.md), will reappear on the roadmap when epic'd. Per ADR-0006.

### Added
- **Documentation guide (`docs/docs-guide.md`)** (#171, epic #169): a contributor-facing
  "where does this go?" reference — one responsibility per doc (README=intro, status=now/SSOT,
  roadmap=future-only/generated, CHANGELOG=history, ADRs=decisions), a decision guide, and the
  enforcement summary. Linked from README; README's roadmap pointer corrected ("what works today vs
  planned" → "planned or in progress, future only"). Per [ADR-0006](docs/adr/0006-docs-ia-and-drift-proof-roadmap.md).
- **ADR-0006 — documentation IA + drift-proof generated roadmap** (#170, epic #169): records the
  root cause of recurring doc drift (the audit has no notion of "realized"; prune-on-close is a
  manual, gateless step) and the fix — one responsibility per doc (README=intro, status=now/SSOT,
  roadmap=future-only, CHANGELOG=history), a **generated** roadmap (from open top-level epics, so a
  closed epic auto-drops), prune-on-close as a C2 gate, and a two-layer audit (offline
  forward-only/per-doc lint + a generation check). Implementation follows in #171–#176.

## [0.0.13] - 2026-06-22

### Added
- **ADR-0005 + roadmap phase 6** (#161, epic #146): records the discovery + per-item invocation +
  `/catalogue` design as [ADR-0005](docs/adr/0005-prompt-skill-discovery-invocation.md) (closing the
  [ADR-0003](docs/adr/0003-prompt-library.md) D5 gap), and adds roadmap **phase 6** "Skill & prompt
  library — usability & content" (the shipped usability/seed foundation + the forward-looking
  curation/maintenance flow). roadmap.md phase 3 marked delivered.
- **Three curated prompt-library seeds** (#150, epic #146): `pr-description`, `refactor-plan`, and
  `test-plan` (engineering, EN+DE) bring the curated starter set under `skills/prompts/` to
  **7**. Each is one declarative `kind: prompt` MD file (+ a `locales/de.json` overlay), passes
  `ack.gate`, and is visible via `/prompts` + invocable via `/<name>` purely by dropping the file —
  no engine change. `test_prompt_library.py` now parametrises over all 7 (24).
- **Catalogue endpoint + dynamic slash autocomplete** (#149, epic #146): a guarded `GET /catalogue`
  serves the loaded registry snapshot (the same `_catalogue_snapshot` that backs `/prompts`/`/skills`
  — one surface). It is gated like `/tasks`/`/doctor` (added to `GATED_PATHS`; 401 without the
  deployment secret under token/sealed). The TypeScript client fetches it lazily on the first
  slash-menu open and merges loaded **prompt** names into autocomplete as directly-invocable
  `/<name>` entries — a built-in command wins on a name collision; skills (not bare-slash invocable)
  are intentionally not injected. Fail-soft: an older server / gated session → built-in commands
  only. `test_catalogue_endpoint.py` (3) + `catalogue.test.ts` (6).
- **Per-item prompt invocation `/<prompt-name>`** (#148, epic #146): the command router resolves a
  `/<prompt-name>` against the loaded prompt catalogue and runs it deterministically (model-free) —
  parses `var=value` / a single positional / `--lang xx`, and reuses the `ack.promptgen` elicitation
  state machine: assembles the finished prompt in the target language when all required variables are
  present, else returns the guiding questions for what is missing. Resolution runs **after** every
  built-in command, so a real command is never shadowed; an unknown `/x` still falls through to a
  model turn. The model-elected `use_prompt` tool stays available (additive). A single positional value fills the
  lone required variable verbatim (a `=`/`--lang` inside a code/diff value is preserved), with a
  trailing `--lang xx` peeled; explicit `var=value` sets named variables. `test_prompt_invocation.py` (14).
- **Discovery commands `/prompts` + `/skills`** (#147, epic #146): read-only listing of the **one
  loaded registry** (no re-scan, no parallel mechanism). `/prompts` lists every loaded
  `kind: prompt` item (name, declared languages, description); `/skills` lists every loaded skill
  across both kinds — `SKILL.md` playbooks and typed `CASE`+`run` tools (incl. the MPR built-in).
  Both are advertised in `/help` and offered in the TypeScript client's slash autocomplete. Backed
  by a shared `_catalogue_snapshot` helper over `_PROMPTS`/`_PLAYBOOKS`/`_PLUGIN_TOOLS`.
  `test_discovery_cmds.py` (6) + `classify.test.ts`.

## [0.0.12] - 2026-06-22

### Changed
- **Internal DEV→Prod→Public promote finalized** (#40, epic #132): the release pipeline now runs as
  a single fail-closed gated flow — boundary → **full** test suite (incl. the MPR built-in, which
  the previous step missed) → docs gate (CHANGELOG + doc-reality-audit) → export gates →
  **PRE-publish clean-room** (wheel → fresh venv → import-smoke incl. `ack.sdk` → an example plugin
  builds against the installed SDK) → review → publish → prod redeploy; dry by default. Full
  automation (scheduled sync) deferred. (Core-maintainer machinery; downstream users never touch it.)

### Added
- **Example plugin + SDK clean-room guarantee** (#138, epic #132): a standalone example plugin
  (`examples/example-plugin/`) shows the separate-repo authoring shape — a package with a `skills/`
  dir + an `ironclad.plugins` entry point, built against `ironclad-ai`. The clean-room now
  import-smokes `ack.sdk` and **builds the example against the freshly-installed wheel**, asserting
  it registers its entry point, runs, and matches the SDK schema — proving a separate repo can build
  against the published artifact. 3 tests (skip in installed trees; the workflow covers that path).
- **Export-leak guard for internal plugins** (#137, epic #132): the boundary check + export
  secret-sweep now forbid the internal plugin repo name, and a leak-guard test pins the guarantee
  (the guards flag a synthetic leak; the real `core/` + `clients/ink` tree is clean). `core/`
  couples to plugins only via the generic `ironclad.plugins` entry-point group — never a concrete
  private plugin. (The guards live in `scripts/ci/`, private; the test skips in installed/clean-room
  trees where they're absent.) 4 tests.
- **Packaged-plugin loading via entry points** (#136, epic #132): a pip-installed plugin
  (3rd-party or internal) is discovered at startup through the `ironclad.plugins` **entry-point
  group** — additively alongside built-ins + `GX10_PLUGINS_DIR`, with no path config and no core
  change. Dependency-inverted: the engine resolves each entry point to a plugins dir
  (package / callable / path) and scans it; it **never imports a concrete plugin**. Broken entry
  points are fail-soft. [ADR-0004](docs/adr/0004-extension-sdk.md), `plugin-api.md`. 10 tests.
- **Extension SDK (`ack.sdk`)** (#72, epic #132): a curated, versioned import surface to build a
  plugin in a **separate repo** against `pip install ironclad-ai` — re-exports the tool/playbook/
  prompt kinds, the registration/eval `gate`, `derive_tool_schema`, `Localizer`, and the
  `catalogue`. `ack.sdk.__all__` **is** the public API; everything else under `ack.*`/`engine.*`
  is internal. Provisional while `0.0.x`, semver from 1.0. [ADR-0004](docs/adr/0004-extension-sdk.md)
  + `plugin-api.md` (separate-repo workflow). 7 tests. *(The contract modules already shipped in
  the `ironclad-ai` wheel; this adds the curated surface, the stability policy, and the docs — no
  distribution change. The packaged-plugin `ironclad.plugins` entry-point seam is in development, #136.)*

## [0.0.11] - 2026-06-21

### Added
- **Curated multilingual starter prompt library + eval gate** (#111): four built-in `kind: prompt`
  items ship under `skills/prompts/` — `code-review`, `commit-message`, `bug-report`,
  `explain-code` (EN + DE, each loaded at startup and offered via `use_prompt`). New
  `ack.gate.gate_prompt` is the registration/eval gate for prompt items (required vars must appear
  in the template; every declared language must assemble cleanly; present locale overlays validated)
  and `ack.gate.gate()` auto-routes `kind: prompt` SKILL.md here. `discover_playbooks` now skips
  prompt items cleanly. **A new prompt = drop an MD file, no engine change.** 20 tests.
- **Prompt slash surface & guided elicitation** (`use_prompt`, #110): a discovered `kind: prompt`
  item is exposed as an engine tool — call with no capability to **list**, or with a capability +
  a `values` JSON of what's collected so far to drive **guided elicitation** (the tool returns the
  next missing required variable's question, one at a time) and, once complete, the **assembled**
  prompt in the target `lang` (preview). Wired through `_load_skills`/`_effective_tools`/dispatch;
  the state machine is `ack.promptgen.run_prompt` (deterministic, LLM-free). 9 tests.
- **Multilingual prompt assembly** (`ack.promptgen`, #109): `assemble(prompt, values, lang)`
  renders a `kind: prompt` template + collected values into a finished prompt in a target
  language via `ack.i18n` (per-item `locales/`, source/target + fallback); `missing_required()`
  drives the elicitation loop. Deterministic, LLM-free. 6 tests.
- **Prompt-library item format** (`ack.prompt`, `kind: prompt`, #108): parse/validate/discover a
  declarative prompt item (variables + languages + per-variable elicitation), reusing the shared
  `ack.playbook` frontmatter parser (one parser, no parallel infra). A prompt is a core built-in,
  distinct from `kind: playbook`. 7 tests.
- **Design: prompt library & generator** ([ADR-0003](docs/adr/0003-prompt-library.md) +
  [`prompt-packaging.md`](docs/prompt-packaging.md), epic #105) — a curated, multilingual prompt
  library on the core base: a prompt is a declarative `kind: prompt` core built-in (variables +
  languages + guided elicitation), reusing `ack.playbook`/`ack.catalogue`/`ack.gate`/`ack.i18n`;
  `/<prompt-name>` → elicit → multilingual assembly → preview. Design only — built under epic #105.

## [0.0.10] - 2026-06-21

### Changed
- **Export/deploy/docs aligned to core MPR** (#116): `export_core.py` drops the separate MPR
  bundling (MPR ships via the core/ copy + is covered by the core boundary check); the install
  launchers no longer set `GX10_MPR`; the installer no longer copies a separate `skills/mpr`; the
  MPR README documents the single runtime `mpr.enabled` gate. Finalizes the `GX10_MPR` deprecation.
- **MPR is now a core, always-on built-in** (#115): moved `skills/mpr` → `skills/mpr`;
  removed the `GX10_MPR` load gate (MPR is always loaded) — the live on/off is the runtime
  config **`mpr.enabled` (default ON)**. MPR consumes the core registry + `ack.i18n` + catalogue
  + gate. Back-compatible (behavior unchanged when enabled); its suite (381) is now part of the
  core `pytest` run, so the private CI gates it too. **Deprecation:** `GX10_MPR` is gone — use
  `mpr.enabled` (or `GX10_MPR_ENABLED`) instead.

### Added
- **Always-on core built-in loader** (#114): built-in skills/prompts load at startup from a
  fixed core dir (`skills/`), scanned **unconditionally** — independent of
  `GX10_PLUGINS_DIR`, which stays the **additive** surface for 3rd-party/user skills
  (`_load_skills`). Built-ins now work out of the box with no config. 4 tests.
- **Shared content i18n `ack.i18n`** (#107): the file-overlay locale loader is promoted to core as
  `Localizer(locales_dir)` — flag-independent (always importable, no `GX10_MPR`/plugin coupling),
  parameterized locales dir, English fallback. MPR migrated onto it (`skills/mpr/i18n.py` is now a
  thin shim; 382 tests green, behavior unchanged). Distinct from `engine/messages.py` (engine
  chrome). 6 tests. Part of the core-always-on rebuild (ADR-0002).
- **Design: skill/prompt/MPR as core always-on** ([ADR-0002](docs/adr/0002-core-always-on-skills.md),
  epic #112) — built-ins load from a fixed core dir independent of `GX10_PLUGINS_DIR`; the plugin
  surface stays for 3rd-party skills; MPR de-plugined into core (runtime `mpr.enabled`, default on,
  replacing the `GX10_MPR` boot flag). Design only — implemented under epic #112.

## [0.0.9] - 2026-06-21

### Added
- **Skill lifecycle verified end-to-end** (#88): a model-free integration test drives the full
  pipeline for both kinds — `ack.skillgen` generate → `ack.gate` registration gate →
  `ack.catalogue` install/register → engine load (`_load_plugins`/`_load_playbooks`) → invoke
  (a typed tool returns a real result; a playbook loads via `use_skill`). This is the epic #22
  C2 runnable scenario. 3 tests.

### Changed
- **`mpr` migrated as the reference built-in** (#90): its `CASE` now carries the catalogue
  manifest fields (`type`/`version`/`provenance`), so `ack.catalogue` indexes it as a built-in
  skill — proving the generalized format is a superset of the real flagship. Additive +
  back-compatible (byte-identical when gated off; mpr suite 382 green).

### Added
- **Skill registration gate** (`ack.gate`, #34): no unchecked skill enters the toolset. A tool
  must pass a doctor preflight (loadable, `CASE`+`capability`, synchronous `run`, derivable tool
  schema) and ship an auto-generated test; a playbook must have valid `SKILL.md` frontmatter +
  readable references + a passing `scripts/check`. Behavioral `eval/` stays opt-in. 7 tests. Also
  made the scaffolded playbook `scripts/check` self-contained (no import path assumptions).
- **Skill library catalogue** (`ack.catalogue`, #35): a self-hosted, versioned index over both
  skill kinds, reading each skill's own metadata as its manifest (`capability`/`kind`/`version`/
  `type`/`domain`/`provenance`/`source`). Discover, install (copy into the active `skills/`),
  and update-when-newer (semver), with provenance and built-in vs user libraries — no external
  marketplace. Zero new deps; 6 tests.
- **Skill generator** (`ack.skillgen`, #33): `spec → schema-valid scaffold` for both skill
  kinds — a typed `CASE`+`run` `.py` (signature → tool schema) with an auto-test stub, or a
  `SKILL.md` playbook package + `references/` + `scripts/check`. Contract-correct by
  construction; the body is a marked stub for an author/LLM to fill. CLI `python -m ack.skillgen`.
  Zero new deps; 7 tests.
- **Playbook skill kind** (`SKILL.md` packages, ADR-0001 / #89): a second skill kind alongside
  the typed `CASE`+`run` tool. `ack.playbook` parses + validates `SKILL.md` frontmatter and
  `Registry.discover_playbooks` discovers packages; the engine exposes them via the new
  **`use_skill`** tool with **progressive disclosure** (list metadata → load body → load a
  reference on demand). Zero new dependencies; 15 deterministic tests (`test_playbook.py`).
- **Skill-engine design**: [ADR-0001](docs/adr/0001-skill-engine-and-library.md) +
  [`skill-packaging.md`](docs/skill-packaging.md) — the design for the skill-generation engine
  & self-hosted library (two skill kinds: typed `CASE`+`run` tools and `SKILL.md` playbooks;
  doctor+tests registration gate with opt-in behavioral eval; manifest catalogue with semver +
  provenance; `skills/mpr` as the reference built-in). Design only — built under epic #22.

## [0.0.8] - 2026-06-21

### Added
- **Public-release clean-room gate** (`.github/workflows/clean-room.yml`, #58). Before any
  publish, the package is proven installable + runnable **from the published sources in
  isolation**: PRE-publish builds the wheel, installs it into a fresh venv (not `-e`),
  import-smokes from a neutral dir and resolves the `[engine]`/`[memory]` extras, and does a
  fresh `clients/ink` build + test; POST-publish installs the real package from PyPI and
  import-smokes. Catches forgotten dependencies/files before a user hits them.

### Fixed
- **Published doc link** (`engine/README.md` → the bundled `clients/ink/` client) now
  resolves in the released tree; the export now normalizes the `../` depth of links to the
  bundled siblings (`clients/ink`, `skills/mpr`), and a deterministic doc-reality audit gate
  guards against dead links/anchors, version drift, and stale claims going forward (#41).

### Changed
- **Docs: release-status & test counts reconciled to reality** (#59). The status/honesty
  sections (`README.md`, `docs/status.md`, `docs/roadmap.md`, `SETUP.md`) now state the
  actual release model — pre-release `0.0.x` alpha, tagged releases on PyPI (`ironclad-ai`)
  **and** GitHub Releases (`v0.0.7`), no stable 1.0 — instead of the stale "no tagged
  release". Test counts regenerated from `pytest --collect-only`: **468** Python
  (459 offline + 9 live) and **337** TypeScript client tests; `docs/test-report.md` area
  table updated to match.
- **English-only hygiene**: translated the remaining German code comments in the bundled
  test suite (`ack/tests/test_autoplan.py`, `test_client_pool.py`, `test_workers.py`) to
  English (#78). Deliberate `language=de` user-facing output and the German query-classifier
  keywords are intentionally kept.

## [0.0.7] - 2026-06-21

### Changed
- **Installer is type-aware + config-driven.** The `ironclad`/`ironclad-doctor` launchers now support both a
  local engine (`type: desktop`, default) and a thin client against a remote orchestrator (`type: spark`),
  and read optional tuning (`engineConfig`, `warmUrl`, `claudeBin`, `fanoutConcurrency`, `workersMaxTokens`,
  `workersMaxBatchTokens`) from the project config — absent → engine defaults, so a deployment can tune
  without editing scripts. Adds `install/ironclad-commands.ps1` (profile shim). All still secret-free.

## [0.0.6] - 2026-06-21

### Added
- **One-shot desktop installer** (`install/`, cross-platform, secret-free). Run `install/ironclad-install.sh`
  (Linux/macOS) or `install\ironclad-install.ps1` (Windows) once from a clone to build a venv, install the
  engine, build the optional TypeScript client, write a per-project config and wire an `ironclad` command —
  plus `ironclad` (launcher: engine-ensure + client, version-aware) and `ironclad-doctor` (status). All
  endpoints default to localhost and are overridable via flags / `GX10_*` env; nothing about a deployment is
  baked in. See [`install/README.md`](install/README.md), the *One-shot install* block in
  [`SETUP.md`](SETUP.md), and **Track D** in [`AGENTS.md`](AGENTS.md) (AI-agent install runbook).

### Changed
- The boundary check (`scripts/ci/check_core_boundary.py`) now also literal-scans `.ps1`/`.sh`, so the
  installers are held to the same secret-free contract as the rest of `core/`.

## [0.0.5] - 2026-06-21

### Changed
- **MPR is multilingual.** The deterministic report rendering (decision-matrix / comparison-matrix /
  risk-register / evidence-report templates), the synthesis prompts and the degrade/panel messages are
  now localized: **English is the source**, German ships as a locale overlay (`skills/mpr/locales/`),
  and the render language follows the configured output language. Adding a language is a data file, no
  code change.
- **Orchestrator system prompt hardened.** It now self-invokes plugin tools when a request matches a
  loaded tool (instead of telling the operator to type commands), never guesses initiative names from
  memory (the active initiative comes from state), and never invents command syntax.

### Fixed
- Python 3.10 compatibility for the bundled MPR plugin: its eval gate read TOML via the stdlib
  `tomllib` (3.11+ only). It now falls back to the `tomli` backport (declared as an `[engine]`
  dependency on `python_version < "3.11"`), so MPR loads and its tests pass on 3.10.
- **MPR decision matrix self-consistency:** the recommendation rationale no longer restates invented
  weighted sums — only the MPR-computed score is shown; the fallback line no longer doubles the trigger
  conjunction or the terminal period.
- **MPR conflict detection** no longer emits noise zones from query meta-vocabulary (question words,
  structural terms, criteria names) nor a fabricated top-recommendation conflict; inferred subjects are
  anchored to the question and de-duplicated.
- **MPR run indexing** in a reasoning-only initiative no longer attempts (and silently fails) a TaskStore
  write — consistent with the initiative type contract; the run's record is its manifest + the vault index.
- **MPR TaskStore binding** in the engine glue is fixed (it bound a function object, never an instance), so
  runs in a software initiative are indexed correctly.
- **CLI slash-command menu:** Tab and Enter accept the highlighted suggestion (a single match too), and an
  arrow key accepts a lone suggestion; the renderer's focus manager no longer swallows Tab.
- **CLI:** the MPR report's machine sentinels (`<<<MPR_REPORT>>>` / `<<<END>>>`) are stripped from the
  rendered chat.

## [0.0.4] - 2026-06-20

### Added
- **Initiative-centric state layout.** Engine machinery moves out of the project root into a hidden
  `state_root` (`.ironclad/`: `session.json`, the local warm-cache, the `active` marker) and every
  produced artifact lives under the **active initiative** `vault/<slug>/` — visible `decisions/`,
  `proposals/`, `reviews/`, `runs/`, `tasks/`; hidden `.work/` machine plumbing (active handover,
  handover/feedback inbox, archive). Initiative are created explicitly
  (`/initiative new|list|use|active|reconcile`, `--type mpr|software`); the `TaskStore`,
  `stage_handover`/`advance_pipeline`, reviews and the MPR `runs_dir` all route to the active
  initiative, **fail-closed** when none is active (no writes into the project root), while background
  scanners soft-skip. The local code-agent scratch moves to a hidden `.ironclad/agent/` drop zone.
  Overridable via `paths.state_root` / `paths.vault_root`. See [`docs/state-and-initiative.md`](docs/state-and-initiative.md).
- **Self-maintaining vault** (`reconcile_vault`, `/initiative reconcile`): deterministic, **LLM-free**
  upkeep — a regenerated `INDEX.md` (grouped, Obsidian `[[links]]`, manual prose preserved outside the
  AUTO block) plus an idempotent "Verwandt (auto)" relation block injected into curated docs
  (shared frontmatter tags / title reference). Auto-fires index-only after a write (initiative create,
  `stage_handover`, `advance_pipeline`, an MPR run); the full link pass runs on the explicit command.
- **MPR multi-perspective reasoner** (`skills/mpr/`) now ships in the OSS as the flagship plugin
  example: an expert role-panel router → governed fan-out → deterministic synthesis (decision-matrix /
  comparison / risk / evidence templates) with a sovereignty/budget-gated audit trail. Loaded via the
  open plugin surface (`GX10_PLUGINS_DIR`), runtime-gated by `mpr.enabled` (default off), used through a
  `--type mpr` initiative. See [`skills/mpr/README.md`](skills/mpr/README.md).
- **Operator security guide** ([`docs/security.md`](docs/security.md)): the trust profiles
  (`open`/`token`/`sealed`), their config keys + env overrides, the gated routes, the session
  lifecycle, and the client header contract — in one place.

### Changed
- `session.json` now lives under `.ironclad/` (was `.gx10_session.json` in the project root).
- Default workspace no longer scatters `tasks/`/`summaries/`/`reviews/` into the project root.
- **Warm tier wired by default** — the shipped `docker-compose` now sets `GX10_WARM_URL` to the
  `mem-valkey` loopback, so the warm tier (rolling summary + retrieval cache) activates automatically
  under `--profile memory` (fail-soft otherwise); previously the container shipped but was never wired.
- **`security.profile` is now a boot-only (frozen) config key** — `/config set security.profile …` is
  refused (it wires the trust policy + bind host once at boot); set it in the deploy and restart.
- **Plugin tool names must be unique** — a name clash now keeps the first-loaded tool and warns,
  instead of silently shadowing it.
- **Docs reconciled against the code** (a 10-dimension docs↔code audit): `status.md`, `setup-types.md`,
  `config-runtime.md`, `plugin-api.md`, `code-agents.md`, README/SETUP/AGENTS — corrected over-claims
  (the boundary check does not sandbox plugins), completed the env/config surface, added the
  `/initiative new` onboarding prerequisite, and refreshed the test counts.

## [0.0.3] - 2026-06-19

### Added
- **Scalable-context memory** (multi-tier, default-on when a memory/warm service is configured):
  a **warm tier** (BSD-licensed in-memory store) holding the rolling conversation summary,
  recent-turn state, and a short-TTL retrieval cache that survives a server restart and is shared
  across the reasoning workers; **rolling/hierarchical summarization** on eviction (raw archived
  to long-term, prefix-stable); **per-turn retrieval (RAG)** assembly; **token-accurate budgeting**
  that scales the working set to the model window; **chunked, lossless** long-artifact store with
  recency tie-breaking; a **`deep_query_memory`** tool for the relational (graph) path; and
  **parallel workers as memory citizens** (shared summary + per-item retrieval on read, single-writer
  reducer on write). All additive and fail-soft — with the warm/cold tiers down, a turn still
  completes. Server-side only (same HTTP contract, no new client coupling).
- **Recommended TypeScript terminal client** (`clients/ink/`) on a purpose-built renderer:
  slash-command autocomplete, local shell via `!cmd`, in-CLI `/update` (rebuild + reinstall),
  `/reset` + opt-in `/resume`, per-project session storage (`<codedir>/.ironclad-cli/`), and
  preserved + syntax-highlighted code display (`/cat` fences with the language from the extension).
- **Secure, session-gated channel** (single-tenant): selectable trust profiles
  `open` / `token` / `sealed`, a client-managed tunnel option, and an explicit session
  that seals on disconnect. The token is a deployment secret, not a user login.
- **Governed reasoning parallelism**: a fan-out governor (concurrency × max_tokens budget
  envelope) plus an in-engine `parallel_reason` tool. Conservative core defaults.
- **Function-calling robustness**: validate→reask on every tool argument, and recovery of
  tool calls from text for endpoints without native tool-calls (explicit markers only).
- **Runtime contract self-check**: `GET /doctor` + a boot summary.
- **Open plugin surface**: discover `skills/*.py` plugins from `GX10_PLUGINS_DIR` and
  expose each as an agent tool — no core change. See `docs/plugin-api.md`.
- **Pluggable code-agent CLI** via `GX10_AGENT_CMD` (not locked to Claude Code).
- **Dev environment**: `Dockerfile.dev` + `docker-compose.dev.yml` build + run the full
  test suite in a container (the build+test gate). See `docs/dev-environment.md`.
- Beginner on-ramp: `docs/self-maintenance.md` ("describe an idea, let the agents build
  it"), top-of-README quickstart.

### Fixed
- Headless code-agent could not write files without a permission mode (now
  `--permission-mode`, default `acceptEdits`).
- `/tasks` was readable without the deployment secret under the auth profiles (now gated).
- Tool-call text recovery could hijack a bare JSON answer into a destructive call
  (bare-object branch removed; explicit markers only).
- Auth-gate / router path normalization; request-body cap; tunnel child reaped on failure;
  config-tree skips hidden subdirs; UTF-8-safe output (no cp1252 crash).

### Notes
- Single-tenant by design; multi-user identity/authorization is not built (see
  `docs/roadmap.md`). Treat `main` as a development snapshot.

## [0.0.2] - 2026-06-18

### Added
- Bundled **TypeScript terminal client** (`clients/ink`): purpose-built renderer (ghost-free resize,
  native scrollback/selection/copy, live-streaming markdown, Ctrl+F search), global install
  (`npm install -g .` → `ironclad`), JSON config file (file < env < flags), Esc/Ctrl+C turn-cancel.

### Changed
- Docs reconciled to the recommended-client + global-install flow; onboarding paths made
  export-relative. The Python ACK + orchestration engine are unchanged in behaviour (version bump only).

## [0.0.1] - 2026-06-17

### Added
- First public pre-release: reliability for LLM agents through enforcement, not model size — the
  Agent-Contract-Kernel + a fail-closed orchestration engine, server/client split, full-screen TUI,
  reasoning-worker fan-out, optional Mem0 memory, and a one-command compose. Model-agnostic (any
  OpenAI-compatible endpoint); PyPI name `ironclad-ai`. See `docs/status.md` for per-component wiring.
