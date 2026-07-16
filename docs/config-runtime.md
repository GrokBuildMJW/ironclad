# Runtime config control — `/config get` & `/config set`

ironclad merges its configuration once at startup with the precedence

```
code-defaults  <  file / conf  <  env
```

into a single in-memory tree (`gx10._EFFECTIVE_CFG`). `/config get` and `/config set` let an operator
**read and override** any key of that tree **at runtime**, without restarting the server. (The standalone
launcher also accepts a few CLI flags like `--model`/`--workdir`; the **headless server** — which is what
actually builds `_EFFECTIVE_CFG` — takes its config from defaults + file/conf + env only.)

These commands are **generic and plugin-agnostic** — core carries no knowledge of any specific section.
Any section (core or plugin) that reads `_EFFECTIVE_CFG` can be steered through them.

## Commands

```
/config                      # the full effective config + its source (read-only summary)
/config get <dotted.key>     # read one key, e.g.  /config get ui.max_lines
/config set <dotted.key> <value>
```

`<dotted.key>` is a dotted path into the config tree; intermediate sections are created as needed.
`<value>` is coerced:

| input | becomes |
|-------|---------|
| `on`, `true`, `yes` | `True` |
| `off`, `false`, `no` | `False` |
| an integer literal | `int` |
| a float literal | `float` |
| anything else | `str` |

Example:

```
/config set ui.max_lines 4000
/config set context.token_budget on
/config get context.token_budget        # → context.token_budget = True
```

> `/config get` renders a key whose value is `None` (a legitimate default, e.g. `providers.default_id`)
> the same as a truly absent key — both show `(not set)`; it does not distinguish them.

## Semantics

1. `set` deep-clones `_EFFECTIVE_CFG` and writes the coerced value only into that unpublished candidate.
2. The complete candidate is schema-validated, including every core leaf and cross-leaf relationship, while
   already-loaded plugin roots remain opaque plugin-owned mappings. Core globals and the intended
   hook/store/thread configuration are then fully derived without publishing any candidate state.
3. Under the config lock, Ironclad commits the derived globals and applies reversible integration wiring.
   `_EFFECTIVE_CFG` is swapped to the candidate only after every core apply step succeeds. A plugin write
   therefore becomes visible to its owner on the next read only after the same atomic commit.
4. Any validation, derivation, commit, or integration error restores the exact prior globals, hooks, stores,
   and worker references, retains the original config tree, and prints one red refusal. A failed core apply
   never degrades into a plugin-only store and never prints a green success line.
5. With no live config yet (`_EFFECTIVE_CFG is None`, before startup has published its merged config), `set`
   is refused without changing state.

Startup keeps a direct apply path because it begins from fresh process state. Config discovery first merges
all sorted top-level files, includes, and nested domain trees into one projection; that final merged tree is
validated before any runtime state or `_EFFECTIVE_CFG` is published.

## Frozen (boot-only) keys

Some keys wire something at **startup** that a later write cannot re-thread. The frozen set is exactly the
typed schema's **`boot_only`** leaves: `_FROZEN_CONFIG_KEYS` is derived from `config_schema` (and equals
`command_spec.SPEC_FROZEN_CONFIG_KEYS`), so it is authoritative and cannot drift from the schema. It covers,
among others, **`setup.type`** (selects the offload runner — see [`setup-types.md`](setup-types.md)),
**`server.host`**, **`security.profile`**, and **`security.allow_unauthenticated_bind`** (build the bind
and trust policy — see [`security.md`](security.md)), **`security.web_in_sealed`** (the sealed-profile web-search opt-in), and
**`search.enabled` / `search.adapter` / `search.api_key_env`** (the boot-wired web-search seam) — plus the
whole **`memory.*`** and **`warm.*`** tiers, the **`workers.concurrency` / `max_tokens` / `max_batch_tokens`**
budgets, the **`providers.*`** CLI-pool wiring, the session **`security.token_env` / `session_heartbeat_s` /
`code_locality`**, and the deployment **`paths.*`** (state/vault/code roots, session file, plugins dir).
Mutating a frozen key at runtime would be incoherent, so `/config get <key>` still reads it but
`/config set <key> …` is **refused** with a clear message ("boot-only — set it in the deploy"). Run
**`/config keys`** for the live, per-key boot-only flag. Change a frozen key in the config file / env and restart.

> **When does an override take effect?** Core globals: after the successful atomic commit (step 3). Plugin sections: on their
> next read of `_EFFECTIVE_CFG` (most plugins re-read per request, so effectively the next call).
>
> **Exception — budget-derived context sizes.** When `context.token_budget` is on (the default), the live
> trimming thresholds `context.max_ctx_chars` / `context.trim_target_chars` are **derived** from the model
> window. A runtime `/config set` of either key is therefore **refused without mutation** while the budget is
> on; set `context.token_budget off` first (or boot with `GX10_TOKEN_BUDGET=0`) to control the char thresholds
> directly. This avoids a green acknowledgement for a value the live budget derivation would replace.

## Secure server and transport defaults

The fresh server is unauthenticated `open` on loopback only. A non-loopback host under `open` refuses at
boot unless the operator explicitly sets the named dangerous override; `token` and `sealed` may bind a
non-loopback host because both require a deployment secret. These leaves are boot-only.

| Key | Env | Default | Range / meaning |
|---|---|---|---|
| `server.host` | `GX10_SERVER_HOST` | `127.0.0.1` | bind host; CLI `--host` has final precedence |
| `security.allow_unauthenticated_bind` | `GX10_ALLOW_UNAUTHENTICATED_BIND` | `false` | explicitly permits `open` on a non-loopback host; dangerous deployment opt-in |
| `connection.request_timeout_s` | `GX10_LLM_TIMEOUT_S` | `120` | positive whole-request budget |
| `connection.connect_timeout_s` | `GX10_LLM_CONNECT_TIMEOUT_S` | `10` | positive, maximum `120`, and cannot exceed `request_timeout_s` |
| `connection.first_token_timeout_s` | `GX10_LLM_FIRST_TOKEN_TIMEOUT_S` | `600` | positive time-to-first-token/read budget, maximum `1800` |

The connection and first-token split is always finite. `null`, zero, negative, non-finite, or
above-ceiling values are refused by the typed schema.

## Web search (`search.*`)

The `web_search` tool is configured under the `search.*` block; the corresponding `GX10_SEARCH_*`
env vars override it (non-secret knobs only).

| Key | Env | Default | Meaning |
|---|---|---|---|
| `search.enabled` | `GX10_SEARCH_ENABLED` | `false` | explicit master enable (frozen, boot-only) |
| `search.adapter` | `GX10_SEARCH_ADAPTER` | `cli` | `cli` (delegate to a web-capable CLI provider), `brave` (native HTTP, **local setup only**), or `mock` (tests) — frozen, boot-only |
| `search.api_key_env` | — | `GX10_SEARCH_API_KEY` | the **name** of the env var holding the search API key; frozen, boot-only |
| `search.count` | `GX10_SEARCH_COUNT` | `10` | results per native (http) search request |
| `search.max_output_chars` | `GX10_SEARCH_MAX_OUTPUT_CHARS` | `100000` | cap on the model-facing result text |
| `security.web_in_sealed` | — | `false` | opt-in to allow outbound web search under the `sealed` trust profile (frozen, boot-only) |

**The API key is never config.** Its VALUE is read from the environment named by `search.api_key_env`
(default `GX10_SEARCH_API_KEY`) at server boot — the config holds only the name, never the secret. The
native (`brave`) adapter is **local-only**: in `server` mode web search falls back to the `cli` adapter.
If the configured adapter is unusable (e.g. `brave` on a local setup with no key), web search stays **off**
fail-soft (a `[search]` boot note explains why) — the server still boots. Supply the key in the
deployment environment (for a desktop/local setup, the user environment, like `GX10_WARM_URL`).

## Forge (`forge.*`)

Forge reads and mutations are disabled on fresh installs. `forge.enabled=true` (or
`GX10_FORGE_ENABLED=1`) is required in addition to a usable adapter and the existing forge/trust policy;
capability detection alone is not authorization.

| Key | Env | Default | Meaning |
|---|---|---|---|
| `forge.enabled` | `GX10_FORGE_ENABLED` | `false` | explicit enable for the forge tool surface |
| `forge.adapter` | `GX10_FORGE_ADAPTER` | `cli` | `cli`, `native`, or `mock` transport |
| `forge.repo` | `GX10_FORGE_REPO` | `""` | optional repository selector |
| `forge.token_env` | `GX10_FORGE_TOKEN_ENV` | `GX10_FORGE_TOKEN` | env-var name holding the native adapter token |

## Loop-intelligence configuration (`ace.*` / `quality.*` / `strategy.*` / `process.*` / `loop_profiles`)

ACE's PlaybookStore provider and reflection path are always on; legacy lesson files remain one-way migration
input. Process hint retrieval is optional and default off. The output-quality breaker and finite failure
strategy are always on:
consecutive sub-threshold verifier scores latch a pre-write staging hold, and a later passing-quality
submission or `/quality reset` clears it, while repeated coder failures consume a bounded per-task budget and
end in a durable `blocked` task with `blocked_kind="escalated"`. These settings have no env override; bounded
`quality.*` changes rebuild the live breaker atomically, retain scores compatible with the configured window,
and recompute the trip state immediately. `strategy.budget` tunes the retry protection but cannot disable it. See
[`status.md`](status.md) for the honest wiring status of each, and [`lesson-api.md`](lesson-api.md) for the
lesson provider API.

| Key | Default | Meaning |
|---|---|---|
| `ace.max_bullets` | `200` | per-scope cap for the always-on ACE playbook; supersedes `lessons.max_per_scope` |
| `quality.threshold` | `0.5` | a verifier score below this counts as a low sample; an at/above-threshold score clears a latched staging hold |
| `quality.min_consecutive` | `3` | consecutive low samples that latch the mandatory pre-write staging hold |
| `quality.window` | `20` | rolling number of scores retained |
| `strategy.enabled` | retired | **Deprecated tombstone.** Either legacy value warns and is ignored; `/config set` refuses it because failure classification, attempt accounting, and terminal escalation are always on |
| `strategy.budget` | `3` | positive finite integer attempt budget; hard maximum `3` |
| `process.hints_enabled` | `false` | inject a pre-turn hint from typed entries in the always-on ACE provider; controls reads only, not ACE writes |
| `process.max_hints` | `3` | max working-approach hints folded into the pre-turn prefix |
| `loop_profiles.default` | `{}` | per-run loop-budget overrides (`max_iterations` / `retry_budget` / `effort`); empty ⇒ the engine globals apply (the live chat-loop bound) |
| `loop_profiles.by_type` | `{}` | per-`TaskType` overrides; `retry_budget` is consumed by code-agent failover and remains capped by the hard retry ceiling, while other per-type loop fields are reserved |

Autonomous external-writer budgets cannot use zero as “unlimited”:

| Key | Env | Default | Allowed range |
|---|---|---|---|
| `workers.concurrency` | `GX10_FANOUT_CONCURRENCY` | `4` | `1..64` |
| `autopilot.max_concurrent` | — | `1` | `1..16` |
| `autopilot.autoplan_max_tasks` | `GX10_AUTOPILOT_MAX_TASKS` | `20` | `1..100`; `/auto on N` and `/autoplan on N` apply the same validation |
| `autopilot.extra_args` | — | `[]` | no default permission-bypass argument |

Live changes to `quality.threshold`, `quality.min_consecutive`, or `quality.window` retain the bounded score
history and the breaker's current latched/recovered state. An unlatched rebuild evaluates only the trailing
low-score streak under the new rule, so a low run that was followed by recovery cannot re-latch it.

## Task heartbeat (`heartbeat.*`)

## Typed core schema

Core configuration is defined by the stdlib-only `engine/config_schema.py` schema. Each effective leaf
declares its exact Python type, default, lifecycle (`runtime` or `boot_only`), operational classification,
secret/redaction policy, environment parser, and any enum, range, or cross-leaf constraint. Code defaults,
the engine frozen-key set, and the command catalogue boot-only metadata are derived from that schema.

Configuration files and merged dictionaries are type-strict. Boolean leaves accept only JSON booleans:
strings such as `"false"`/`"true"`, integers such as `0`/`1`, and `null` are refused rather than interpreted
through Python truthiness. Environment variables are parsed before schema validation and accept only `true`,
`false`, `1`, `0`, `yes`, `no`, `on`, or `off` (case-insensitive); an invalid spelling is warned and ignored.
`/config set` continues to convert its documented boolean words, then validates the resulting typed value.
Numeric values must be finite and within their declared bounds, and merged projections must satisfy declared
cross-leaf relationships.

Boot-only leaves describe values whose consumers are constructed or frozen during startup. `/config set`
refuses every schema leaf in that lifecycle and directs the operator to change the config file/environment and
restart. Runtime leaves remain live controls. The complete reference below is generated directly from the
schema; retired protections appear only as tombstone metadata and never as live enable rows.

<!-- BEGIN generated: config-leaves -->
<!-- Generated by a private CI generator; do not edit this region. -->
| Key | Exact runtime type | Default | Environment | Lifecycle | Classification | Bounds / enum | Deprecation / alias |
|---|---|---|---|---|---|---|---|
| `ace.cost` | `int` | `1` | — | `runtime` | `tuning` | >= 1 | — |
| `ace.embed_url` | `str` | `""` | — | `runtime` | `tuning` | — | — |
| `ace.fork_mpr.enabled` | `bool` | `false` | — | `runtime` | `switch` | — | — |
| `ace.max_bullets` | `int` | `200` | — | `runtime` | `tuning` | >= 0 | — |
| `ace.rounds` | `int` | `1` | — | `runtime` | `tuning` | >= 1 | — |
| `ace.top_k` | `int` | `8` | — | `runtime` | `tuning` | >= 1 | — |
| `alert.enabled` | `bool` | `false` | `GX10_ALERT_ENABLED` | `boot_only` | `switch` | — | — |
| `alert.interval_s` | `int or float` | `300` | — | `boot_only` | `tuning` | > 0 | — |
| `audit.scope` | `str` | `"mutating"` | `GX10_AUDIT_SCOPE` | `runtime` | `switch` | enum: "mutating", "all" | — |
| `automation.decoupled` | `bool` | `false` | — | `runtime` | `switch` | — | — |
| `autopilot.autoplan` | `bool` | `false` | `GX10_AUTOPILOT_AUTOPLAN` | `runtime` | `switch` | — | — |
| `autopilot.autoplan_max_tasks` | `int` | `20` | `GX10_AUTOPILOT_MAX_TASKS` | `runtime` | `tuning` | >= 1; <= 100 | — |
| `autopilot.claude_bin` | `str` | `"claude"` | — | `runtime` | `tuning` | — | — |
| `autopilot.default_effort` | `str` | `"medium"` | — | `runtime` | `switch` | enum: "low", "medium", "high", "xhigh" | — |
| `autopilot.enabled` | `bool` | `false` | `GX10_AUTOPILOT` | `runtime` | `switch` | — | — |
| `autopilot.extra_args` | `list` | `[]` | — | `runtime` | `tuning` | — | — |
| `autopilot.log_terminal` | `bool` | `false` | `GX10_AUTOPILOT_LOG_TERMINAL` | `runtime` | `switch` | — | — |
| `autopilot.logs_dir` | `str` | `"logs"` | — | `runtime` | `tuning` | — | — |
| `autopilot.max_concurrent` | `int` | `1` | — | `runtime` | `tuning` | >= 1; <= 16 | — |
| `autopilot.stream` | `bool` | `false` | `GX10_AUTOPILOT_STREAM` | `runtime` | `switch` | — | — |
| `autopilot.terminate_on_advance` | `bool` | `false` | `GX10_AUTOPILOT_TERMINATE` | `runtime` | `switch` | — | — |
| `code_agents.classes.analysis` | `list` | `["SONNET"]` | — | `boot_only` | `tuning` | — | — |
| `code_agents.classes.complex` | `list` | `["OPUS"]` | — | `boot_only` | `tuning` | — | — |
| `code_agents.classes.routine` | `list` | `["SONNET"]` | — | `boot_only` | `tuning` | — | — |
| `code_agents.classes.standard` | `list` | `["SONNET", "OPUS"]` | — | `boot_only` | `tuning` | — | — |
| `code_agents.exhausted.exit_codes` | `list` | `[]` | — | `boot_only` | `tuning` | — | — |
| `code_agents.exhausted.json_event_types` | `list` | `[]` | — | `boot_only` | `tuning` | — | — |
| `code_agents.exhausted.stderr_patterns` | `list` | `["(?i)\\b(quota\|usage limit\|rate limit\|insufficient (credit\|balance\|quota))\\b", "(?i)\\b(out of\|exceeded)\\b.{0,24}\\b(quota\|credit\|budget\|tokens?)\\b", "(?i)\\b429\\b.{0,20}too many requests"]` | — | `boot_only` | `tuning` | — | — |
| `code_agents.pinned` | `str or null` | `null` | — | `runtime` | `switch` | — | — |
| `code_agents.pool` | `list` | `[{"agent_id": "OPUS", "bin": "claude", "cmd_template": "{bin} --model {model} --effort {effort} --permission-mode {permission} --print {prompt}", "cost_per_1k_in": 0.015, "cost_per_1k_out": 0.075, "display": "Claude Opus 4.8", "effort": "xhigh", "kind": "cli", "model": "claude-opus-4-8", "permission_mode": "default", "provider_id": "claude-opus"}, {"agent_id": "SONNET", "bin": "claude", "cmd_template": "{bin} --model {model} --effort {effort} --permission-mode {permission} --print {prompt}", "cost_per_1k_in": 0.003, "cost_per_1k_out": 0.015, "display": "Claude Sonnet 5", "effort": "high", "kind": "cli", "model": "claude-sonnet-5", "permission_mode": "default", "provider_id": "claude-sonnet"}]` | — | `boot_only` | `tuning` | — | — |
| `code_agents.timeout_s` | `int or float` | `1800.0` | `GX10_CODE_AGENTS_TIMEOUT_S` | `runtime` | `tuning` | > 0; <= 7200 | — |
| `connection.api_key_env` | `str` | `"GX10_API_KEY"` | — | `runtime` | `tuning` | — | — |
| `connection.base_url` | `str` | `"http://localhost:8000/v1"` | `GX10_BASE_URL` | `runtime` | `tuning` | — | — |
| `connection.connect_timeout_s` | `int or float` | `10.0` | `GX10_LLM_CONNECT_TIMEOUT_S` | `runtime` | `tuning` | > 0; <= 120; relationship: timeout_relationship | — |
| `connection.first_token_timeout_s` | `int or float` | `600.0` | `GX10_LLM_FIRST_TOKEN_TIMEOUT_S` | `runtime` | `tuning` | > 0; <= 1800; relationship: timeout_relationship | — |
| `connection.max_retries` | `int` | `1` | `GX10_LLM_MAX_RETRIES` | `runtime` | `tuning` | >= 0 | — |
| `connection.model` | `str` | `"qwen3.6-35b"` | `GX10_MODEL` | `runtime` | `tuning` | — | — |
| `connection.request_timeout_s` | `int or float` | `120.0` | `GX10_LLM_TIMEOUT_S` | `runtime` | `tuning` | > 0; <= 3600; relationship: timeout_relationship | — |
| `context.chars_per_token` | `int or float` | `2.6` | `GX10_CHARS_PER_TOKEN` | `runtime` | `tuning` | > 0 | — |
| `context.emergency_summarize` | `bool` | `false` | `GX10_EMERGENCY_SUMMARIZE` | `runtime` | `switch` | — | — |
| `context.ingest_soft_frac` | `int or float` | `0.7` | `GX10_INGEST_SOFT_FRAC` | `runtime` | `tuning` | > 0; <= 1 | — |
| `context.list_dir_hard_cap` | `int` | `200` | — | `runtime` | `tuning` | >= 1 | — |
| `context.max_ctx_chars` | `int` | `80000` | `GX10_MAX_CTX_CHARS` | `runtime` | `tuning` | >= 1; relationship: trim_relationship | — |
| `context.max_file_chars` | `int` | `24000` | — | `runtime` | `tuning` | >= 1 | — |
| `context.max_iterations` | `int` | `20` | — | `runtime` | `tuning` | >= 1 | — |
| `context.max_model_len` | `int` | `32768` | `IRONCLAD_MAX_MODEL_LEN`, `GX10_MAX_MODEL_LEN` | `runtime` | `tuning` | >= 1 | — |
| `context.max_summaries_per_turn` | `int` | `0` | `GX10_MAX_SUMMARIES_PER_TURN` | `runtime` | `tuning` | >= 0 | — |
| `context.memory_brief_tokens` | `int` | `1200` | `GX10_MEMORY_BRIEF_TOKENS` | `runtime` | `tuning` | >= 1 | — |
| `context.min_output_tokens` | `int` | `1024` | `GX10_MIN_OUTPUT_TOKENS` | `runtime` | `tuning` | >= 1 | — |
| `context.overflow_safety_tokens` | `int` | `1536` | `GX10_OVERFLOW_SAFETY` | `runtime` | `tuning` | >= 0 | — |
| `context.proactive_roll` | `bool` | `false` | `GX10_PROACTIVE_ROLL` | `runtime` | `switch` | — | — |
| `context.rag_enabled` | `bool` | `true` | `GX10_CONTEXT_RAG` | `runtime` | `switch` | — | — |
| `context.rag_max_tokens` | `int` | `1024` | `GX10_RAG_MAX_TOKENS` | `runtime` | `tuning` | >= 1 | — |
| `context.rag_top_k` | `int` | `5` | `GX10_RAG_TOP_K` | `runtime` | `tuning` | >= 1 | — |
| `context.summarize_evicted` | `bool` | `true` | `GX10_CONTEXT_SUMMARY` | `runtime` | `switch` | — | — |
| `context.summary_max_tokens` | `int` | `512` | `GX10_SUMMARY_MAX_TOKENS` | `runtime` | `tuning` | >= 1 | — |
| `context.thinking_reserve` | `int` | `4000` | `GX10_THINKING_RESERVE` | `runtime` | `tuning` | >= 0 | — |
| `context.token_budget` | `bool` | `true` | `GX10_TOKEN_BUDGET` | `runtime` | `switch` | — | — |
| `context.trim_target_chars` | `int` | `48000` | `GX10_TRIM_TARGET_CHARS` | `runtime` | `tuning` | >= 1; relationship: trim_relationship | — |
| `context.turn_idle_timeout_s` | `int or float` | `240.0` | `GX10_TURN_IDLE_TIMEOUT_S` | `runtime` | `tuning` | > 0; relationship: timeout_relationship | — |
| `forge.adapter` | `str` | `"cli"` | `GX10_FORGE_ADAPTER` | `runtime` | `switch` | enum: "cli", "native", "mock" | — |
| `forge.enabled` | `bool` | `false` | `GX10_FORGE_ENABLED` | `runtime` | `switch` | — | — |
| `forge.repo` | `str` | `""` | `GX10_FORGE_REPO` | `runtime` | `tuning` | — | — |
| `forge.token_env` | `str` | `"GX10_FORGE_TOKEN"` | `GX10_FORGE_TOKEN_ENV` | `runtime` | `tuning` | — | — |
| `framing_notes.enabled` | `bool` | `false` | — | `runtime` | `switch` | — | — |
| `generation.finalize_on_truncation` | `bool` | `false` | `GX10_FINALIZE_ON_TRUNCATION` | `runtime` | `switch` | — | — |
| `generation.language` | `str` | `"en"` | `GX10_LANGUAGE` | `runtime` | `tuning` | — | — |
| `generation.max_tokens` | `int` | `8192` | `GX10_MAX_TOKENS` | `runtime` | `tuning` | >= 1 | — |
| `generation.retry_backoff` | `int or float` | `1.5` | — | `runtime` | `tuning` | >= 0 | — |
| `generation.stream` | `bool` | `true` | — | `runtime` | `switch` | — | — |
| `generation.temperature` | `int or float` | `0.3` | — | `runtime` | `tuning` | >= 0; <= 2 | — |
| `generation.thinking_mode` | `str` | `"auto"` | `GX10_THINKING` | `runtime` | `switch` | — | — |
| `heartbeat.claim_lease_seconds` | `int or float` | `120` | — | `runtime` | `tuning` | > 0 | — |
| `heartbeat.stall_seconds` | `int or float` | `900` | — | `runtime` | `tuning` | > 0 | — |
| `lodestar.enabled` | `bool` | `false` | — | `runtime` | `switch` | — | — |
| `loop_profiles.by_type` | `dict` | `{}` | — | `runtime` | `tuning` | — | — |
| `loop_profiles.default` | `dict` | `{}` | — | `runtime` | `tuning` | — | — |
| `memory.add_timeout` | `int or float` | `120.0` | — | `boot_only` | `tuning` | > 0 | — |
| `memory.agent_id` | `str` | `"ironclad"` | `GX10_MEMORY_AGENT` | `boot_only` | `tuning` | — | — |
| `memory.base_url` | `str` | `""` | `GX10_MEMORY_URL` | `boot_only` | `tuning` | — | — |
| `memory.chunk_long_artifacts` | `bool` | `true` | `GX10_MEMORY_CHUNKING` | `boot_only` | `switch` | — | — |
| `memory.chunk_overlap` | `int` | `400` | — | `boot_only` | `tuning` | >= 0; relationship: memory_chunk_relationship | — |
| `memory.chunk_size` | `int` | `6000` | — | `boot_only` | `tuning` | >= 1; relationship: memory_chunk_relationship | — |
| `memory.deep_timeout` | `int or float` | `40.0` | — | `boot_only` | `tuning` | > 0 | — |
| `memory.enabled` | `bool` | `true` | — | `boot_only` | `switch` | — | — |
| `memory.health_ttl` | `int or float` | `10.0` | — | `boot_only` | `tuning` | > 0 | — |
| `memory.read_timeout` | `int or float` | `15.0` | — | `boot_only` | `tuning` | > 0 | — |
| `memory.recency_tiebreak` | `bool` | `true` | `GX10_MEMORY_RECENCY` | `boot_only` | `switch` | — | — |
| `memory.user_id` | `str or null` | `null` | — | `boot_only` | `tuning` | — | — |
| `metrics.slo_error_rate` | `int or float` | `0.2` | — | `runtime` | `tuning` | >= 0; <= 1 | — |
| `metrics.slo_p95_latency_s` | `int or float` | `60.0` | — | `runtime` | `tuning` | > 0 | — |
| `metrics.window_s` | `int` | `3600` | — | `runtime` | `tuning` | >= 1 | — |
| `notify.webhook` | `str` | `""` | `GX10_NOTIFY_WEBHOOK` | `runtime` | `tuning` | — | — |
| `onboarding.enabled` | `bool` | `false` | `GX10_ONBOARDING` | `runtime` | `switch` | — | — |
| `paths.active_capability_backlog` | `str or null` | `null` | — | `runtime` | `tuning` | — | — |
| `paths.code_root` | `str` | `""` | — | `boot_only` | `tuning` | — | — |
| `paths.code_subdir` | `str` | `""` | — | `boot_only` | `tuning` | validator: relative_code_subdir | — |
| `paths.plugins_dir` | `str` | `""` | `GX10_PLUGINS_DIR` | `boot_only` | `tuning` | — | — |
| `paths.post_advance_hooks` | `list` | `[]` | — | `boot_only` | `tuning` | — | — |
| `paths.session_file` | `str` | `"session.json"` | — | `boot_only` | `tuning` | — | — |
| `paths.state_root` | `str` | `".ironclad"` | — | `boot_only` | `tuning` | — | — |
| `paths.system_prompt` | `str` | `"prompts/GX10_Orchestrator_SystemPrompt.md"` | `GX10_PROMPT` | `runtime` | `tuning` | — | — |
| `paths.vault_root` | `str` | `"vault"` | — | `boot_only` | `tuning` | — | — |
| `paths.workdir` | `str` | `"."` | `GX10_WORKDIR` | `boot_only` | `tuning` | — | — |
| `platform.mode` | `str` | `"auto"` | `GX10_PLATFORM` | `runtime` | `switch` | enum: "auto", "windows", "linux" | — |
| `process.hints_enabled` | `bool` | `false` | — | `runtime` | `switch` | — | — |
| `process.max_hints` | `int` | `3` | — | `runtime` | `tuning` | >= 1 | — |
| `providers.budget.usd_cap` | `int or float or null` | `null` | `GX10_PROVIDERS_BUDGET_USD` | `runtime` | `tuning` | >= 0 | — |
| `providers.cli_timeout_s` | `int or float` | `900.0` | `GX10_PROVIDERS_CLI_TIMEOUT_S` | `boot_only` | `tuning` | > 0; <= 3600 | — |
| `providers.default_id` | `str or null` | `null` | `GX10_PROVIDERS_DEFAULT` | `boot_only` | `tuning` | — | — |
| `providers.effort_max_tokens` | `dict` | `{"high": 2048, "low": 512, "medium": 1024, "xhigh": 4096}` | — | `boot_only` | `tuning` | — | — |
| `providers.max_agents` | `int` | `3` | `GX10_PROVIDERS_MAX_AGENTS` | `boot_only` | `tuning` | >= 1 | — |
| `providers.pool` | `list` | `[]` | — | `boot_only` | `tuning` | — | — |
| `quality.min_consecutive` | `int` | `3` | — | `runtime` | `tuning` | >= 1; relationship: quality_relationship | — |
| `quality.threshold` | `int or float` | `0.5` | — | `runtime` | `tuning` | >= 0; <= 1 | — |
| `quality.window` | `int` | `20` | — | `runtime` | `tuning` | >= 1; relationship: quality_relationship | — |
| `review.agent` | `str` | `""` | `GX10_REVIEW_AGENT` | `runtime` | `tuning` | — | — |
| `review.timeout_s` | `int or float` | `180.0` | `GX10_REVIEW_TIMEOUT_S` | `runtime` | `tuning` | > 0; <= 3600 | — |
| `search.adapter` | `str` | `"cli"` | `GX10_SEARCH_ADAPTER` | `boot_only` | `switch` | enum: "cli", "brave", "mock" | — |
| `search.api_key_env` | `str` | `"GX10_SEARCH_API_KEY"` | — | `boot_only` | `tuning` | — | — |
| `search.count` | `int` | `10` | `GX10_SEARCH_COUNT` | `runtime` | `tuning` | >= 1; <= 100 | — |
| `search.enabled` | `bool` | `false` | `GX10_SEARCH_ENABLED` | `boot_only` | `switch` | — | — |
| `search.max_output_chars` | `int` | `100000` | `GX10_SEARCH_MAX_OUTPUT_CHARS` | `runtime` | `tuning` | >= 1 | — |
| `security.allow_unauthenticated_bind` | `bool` | `false` | `GX10_ALLOW_UNAUTHENTICATED_BIND` | `boot_only` | `switch` | — | — |
| `security.code_locality` | `str` | `"mount"` | `GX10_CODE_LOCALITY` | `boot_only` | `switch` | enum: "mount", "local" | — |
| `security.multi_tenant` | `bool` | `false` | `GX10_MULTI_TENANT` | `runtime` | `switch` | validator: multi_tenant | — |
| `security.profile` | `str` | `"open"` | `GX10_PROFILE` | `boot_only` | `switch` | enum: "open", "token", "sealed" | — |
| `security.sandbox` | `str` | `"auto"` | `GX10_SANDBOX` | `runtime` | `switch` | enum: "auto", "bwrap", "firejail" | — |
| `security.session_heartbeat_s` | `int` | `30` | `GX10_SESSION_HEARTBEAT` | `boot_only` | `tuning` | >= 5 | — |
| `security.token_env` | `str` | `"GX10_SERVER_TOKEN"` | — | `boot_only` | `tuning` | — | — |
| `security.tooling_envelope.allow_list` | `list or null` | `null` | — | `boot_only` | `tuning` | — | — |
| `security.web_in_sealed` | `bool` | `false` | — | `boot_only` | `switch` | — | — |
| `server.host` | `str` | `"127.0.0.1"` | `GX10_SERVER_HOST` | `boot_only` | `tuning` | — | — |
| `setup.type` | `str` | `"server"` | `GX10_SETUP_TYPE` | `boot_only` | `switch` | enum: "server", "local", "auto" | — |
| `strategy.budget` | `int` | `3` | — | `runtime` | `tuning` | >= 1; <= 3 | — |
| `tasks.dedup_threshold` | `int or float` | `0.8` | — | `runtime` | `tuning` | >= 0; <= 1 | — |
| `tasks.id_prefix` | `str` | `"KGC"` | — | `boot_only` | `tuning` | — | — |
| `thinking_auto.planning_keywords` | `list` | `["\u0065\u0072\u0073\u0074\u0065\u006c\u006c", "\u0070\u006c\u0061\u006e\u0065", "\u0070\u006c\u0061\u006e\u0020", "\u007a\u0065\u0072\u006c\u0065\u0067", "\u0061\u006e\u0061\u006c\u0079\u0073\u0069\u0065\u0072", "\u0065\u006e\u0074\u0073\u0063\u0068\u0065\u0069\u0064", "\u0072\u0065\u0076\u0069\u0065\u0077", "\u0061\u0072\u0063\u0068\u0069\u0074\u0065\u006b\u0074", "\u0064\u0065\u0073\u0069\u0067\u006e", "\u0077\u0061\u0072\u0075\u006d", "\u0077\u0065\u0073\u0068\u0061\u006c\u0062", "\u0076\u0065\u0072\u0067\u006c\u0065\u0069\u0063\u0068", "\u0072\u0065\u0066\u0061\u0063\u0074\u006f\u0072", "\u0069\u006d\u0070\u006c\u0065\u006d\u0065\u006e\u0074\u0069\u0065\u0072", "\u006b\u006f\u006e\u007a\u0065\u0070\u0074", "\u0070\u0072\u006f\u0070\u006f\u0073\u0061\u006c", "\u0068\u0061\u006e\u0064\u006f\u0076\u0065\u0072", "\u0062\u0065\u0077\u0065\u0072\u0074\u0065", "\u0073\u0074\u0072\u0061\u0074\u0065\u0067", "\u0065\u0076\u0061\u006c\u0075\u0069\u0065\u0072", "\u006f\u0070\u0074\u0069\u006d\u0069\u0065\u0072", "\u0062\u0065\u0067\u0072\u00fc\u006e\u0064", "\u0073\u0063\u0068\u006c\u0061\u0067\u0020\u0076\u006f\u0072", "\u0065\u006e\u0074\u0077\u0069\u0072\u0066"]` | — | `runtime` | `tuning` | — | — |
| `thinking_auto.routine_keywords` | `list` | `["\u0077\u0065\u006c\u0063\u0068\u0065", "\u0077\u0061\u0073\u0020\u0069\u0073\u0074\u0020\u006f\u0066\u0066\u0065\u006e", "\u006f\u0066\u0066\u0065\u006e", "\u0073\u0074\u0061\u0074\u0075\u0073", "\u006c\u0069\u0073\u0074\u0065", "\u006c\u0069\u0073\u0074\u0020", "\u007a\u0065\u0069\u0067", "\u00fc\u0062\u0065\u0072\u0073\u0069\u0063\u0068\u0074", "\u00fc\u0062\u0065\u0072\u0062\u006c\u0069\u0063\u006b", "\u0077\u0069\u0065\u0020\u0076\u0069\u0065\u006c\u0065", "\u0073\u0068\u006f\u0077", "\u006f\u0070\u0065\u006e\u0020\u0074\u0061\u0073\u006b", "\u006c\u0069\u0065\u0073\u0020", "\u0063\u0061\u0074\u0020", "\u006c\u0073\u0020", "\u0067\u0069\u0062\u0020\u006d\u0069\u0072", "\u0077\u0065\u006c\u0063\u0068\u0065\u0072", "\u0077\u0065\u006c\u0063\u0068\u0065\u0073", "\u0065\u0074\u0077\u0061\u0073\u0020\u007a\u0075\u0020\u0074\u0075\u006e", "\u007a\u0075\u0020\u0074\u0075\u006e", "\u0073\u0074\u0065\u0068\u0074\u0020\u0061\u006e", "\u0074\u006f\u0064\u006f", "\u0074\u006f\u002d\u0064\u006f", "\u0069\u0064\u006c\u0065", "\u0061\u006e\u0079\u0074\u0068\u0069\u006e\u0067\u0020\u0074\u006f\u0020\u0064\u006f", "\u0077\u0061\u0073\u0020\u006c\u0069\u0065\u0067\u0074\u0020\u0061\u006e", "\u006c\u0069\u0065\u0067\u0074\u0020\u0077\u0061\u0073\u0020\u0061\u006e"]` | — | `runtime` | `tuning` | — | — |
| `ui.max_lines` | `int` | `5000` | — | `runtime` | `tuning` | >= 1 | — |
| `ui.refresh_interval` | `int or float` | `0.1` | — | `runtime` | `tuning` | > 0 | — |
| `ui.spinner_frames` | `str` | `"⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"` | — | `runtime` | `tuning` | — | — |
| `verify.grounding_threshold` | `int or float` | `0.5` | — | `runtime` | `tuning` | >= 0; <= 1 | — |
| `warm.cache_ttl` | `int` | `180` | — | `boot_only` | `tuning` | >= 1 | — |
| `warm.enabled` | `bool` | `true` | — | `boot_only` | `switch` | — | — |
| `warm.session_ttl` | `int` | `86400` | — | `boot_only` | `tuning` | >= 1 | — |
| `warm.timeout` | `int or float` | `0.5` | — | `boot_only` | `tuning` | > 0 | — |
| `warm.url` | `str` | `""` | `GX10_WARM_URL` | `boot_only` | `tuning` | — | — |
| `watcher.feedback_dir` | `str` | `"feedback"` | — | `runtime` | `tuning` | — | — |
| `watcher.interval` | `int or float` | `3.0` | — | `boot_only` | `tuning` | > 0 | — |
| `workers.concurrency` | `int` | `4` | `GX10_FANOUT_CONCURRENCY` | `boot_only` | `tuning` | >= 1; <= 64 | — |
| `workers.max_batch_tokens` | `int` | `8192` | `GX10_WORKERS_MAX_BATCH_TOKENS` | `boot_only` | `tuning` | >= 1; relationship: worker_relationship | — |
| `workers.max_tokens` | `int` | `1024` | `GX10_WORKERS_MAX_TOKENS` | `boot_only` | `tuning` | >= 1; relationship: worker_relationship | — |
| `workers.memory_read` | `bool` | `true` | `GX10_WORKER_MEMORY` | `runtime` | `switch` | — | — |
| `workers.memory_write` | `bool` | `true` | `GX10_WORKER_WRITE` | `runtime` | `switch` | — | — |
| `workers.write_mode` | `str` | `"reducer"` | `GX10_WORKER_WRITE_MODE` | `runtime` | `switch` | enum: "reducer", "direct" | — |
| `workspace.dirs` | `list` | `["vault"]` | — | `runtime` | `tuning` | — | — |
| `workspace.idle_marker` | `str` | `"# Workflow — idle\n\nNo active handover.\n"` | — | `runtime` | `tuning` | — | — |

### Retired keys and one-release aliases

These entries are metadata only and are not effective leaves. Tombstone values are ignored during legacy-file migration and refused by `/config set`; aliases are consumed into their replacement and removed from the effective tree.

| Retired key | Kind | Replacement | Reason |
|---|---|---|---|
| `ace.safe_promote` | tombstone | — | learned-state safety is always on |
| `ack.enabled` | tombstone | — | task validation is always on |
| `advance_gate.enabled` | tombstone | — | completion authority is always on |
| `audit.enabled` | tombstone | — | mutating-action audit is always on |
| `constraint_gate.enabled` | one-release alias | `framing_notes.enabled` | renamed configuration key |
| `design_gate.enabled` | tombstone | — | design lifecycle protection is always on |
| `lessons.enabled` | tombstone | — | ACE is always on through the PlaybookStore provider |
| `lessons.max_per_scope` | tombstone | — | use ace.max_bullets instead |
| `process.enabled` | one-release alias | `process.hints_enabled` | renamed configuration key |
| `providers.enabled` | tombstone | — | setup.type is the single provider-topology authority |
| `providers.scoring` | tombstone | — | router scoring uses fixed built-in constants until a live policy is implemented |
| `quality.enabled` | tombstone | — | the output-quality breaker is always on |
| `safety.ambiguity_detect` | tombstone | — | the no-guessing ambiguity gate is always on |
| `safety.constraint_conflict_detect` | tombstone | — | product constraint-conflict detection remains retired |
| `security.egress_analysis.enabled` | tombstone | — | egress enforcement is always on |
| `security.injection_defense` | tombstone | — | injection fencing is always on |
| `security.tooling_envelope.enabled` | tombstone | — | tooling authorization is always on |
| `strategy.enabled` | tombstone | — | finite failure strategy is always on |
| `verify.enabled` | tombstone | — | handover verification is always on |
| `watcher.enabled` | tombstone | — | /auto on|off is the single watcher authority |
<!-- END generated: config-leaves -->

### External memory and warm-tier seams

These component-owned files and environment inputs are read outside the schema-derived code-default tree.
They remain separate from the effective-leaf inventory so tolerant component overlays are not misrepresented
as strict core schema leaves. The file seams resolve from the process cwd captured when the engine module is
imported, before server bootstrap changes to `paths.workdir`, so every later runtime derivation reads the same
boot/install files.

<!-- BEGIN generated: external-memory-warm-seams -->
<!-- Generated by a private CI generator; do not edit this region. -->
| Seam | Kind | Runtime meaning |
|---|---|---|
| `conf/memory/memory.json` | file | Component-owned MemoryManager overlay; tolerantly merged over the typed `memory.*` block. |
| `GX10_MEMORY_URL` | environment | Overrides `memory.base_url` and enables the cold-memory seam when non-empty. |
| `GX10_MEMORY_AGENT` | environment | Supplies the cold-memory base `agent_id` when `GX10_MEMORY_URL` activates the seam. |
| `conf/warm/warm.json` | file | Component-owned WarmTier overlay; tolerantly merged over the typed `warm.*` block. |
| `GX10_WARM_URL` | environment | Overrides `warm.url` and enables the warm tier when non-empty. |
| `GX10_SESSION_ID` | environment | Selects the warm session key; an empty value resolves to `main`. |
<!-- END generated: external-memory-warm-seams -->

### Operational switch inventory

This generated region is both public documentation and machine input to the switch-parity guard. A switch is
a boolean or enum-like leaf that selects an operational path; tuning leaves adjust bounded behavior without
creating a protection bypass.

<!-- BEGIN generated: config-switches -->
<!-- Generated by a private CI generator; parsed by the switch-parity guard. -->
- `ace.fork_mpr.enabled`
- `alert.enabled`
- `audit.scope`
- `automation.decoupled`
- `autopilot.autoplan`
- `autopilot.default_effort`
- `autopilot.enabled`
- `autopilot.log_terminal`
- `autopilot.stream`
- `autopilot.terminate_on_advance`
- `code_agents.pinned`
- `context.emergency_summarize`
- `context.proactive_roll`
- `context.rag_enabled`
- `context.summarize_evicted`
- `context.token_budget`
- `forge.adapter`
- `forge.enabled`
- `framing_notes.enabled`
- `generation.finalize_on_truncation`
- `generation.stream`
- `generation.thinking_mode`
- `lodestar.enabled`
- `memory.chunk_long_artifacts`
- `memory.enabled`
- `memory.recency_tiebreak`
- `onboarding.enabled`
- `platform.mode`
- `process.hints_enabled`
- `search.adapter`
- `search.enabled`
- `security.allow_unauthenticated_bind`
- `security.code_locality`
- `security.multi_tenant`
- `security.profile`
- `security.sandbox`
- `security.web_in_sealed`
- `setup.type`
- `warm.enabled`
- `workers.memory_read`
- `workers.memory_write`
- `workers.write_mode`
<!-- END generated: config-switches -->

The detect-progress heartbeat is always on with a finite 900-second default. `heartbeat.stall_seconds` and
`heartbeat.claim_lease_seconds` accept only positive finite durations; zero, negative, non-finite, or malformed
values are refused by the typed schema and never disable either protection. Positive finite overrides remain
live operational tuning.

The heartbeat distinguishes **went silent** from **never emitted a signal**. An `in_progress` task is
auto-stalled only after a coder log or feedback file has established observable progress and that newest signal
then ages past the configured duration. A manually managed task with no log or feedback signal ever is
deliberately not auto-stalled, preventing a false positive when its work is not observable to the engine.

The separate 120-second claim lease applies only to client-run tasks stamped by `POST /claim`. The Python thin
client renews that idempotent claim while its coder runs. If the process dies and renewals stop, the reconciler
returns the expired, otherwise-unblocked task to `pending`; unstamped server-launched/autopilot tasks are skipped.

| Key | Default | Meaning |
|---|---|---|
| `heartbeat.claim_lease_seconds` | `120` | positive finite lifetime of a client-run claim since its newest `POST /claim`; expiry returns an otherwise-unblocked task to `pending` |
| `heartbeat.stall_seconds` | `900` | positive finite seconds after the newest observed coder-log/feedback signal before the task is marked `blocked_kind="stalled"`; no signal ever is excluded |

Watcher arming is runtime state, not file configuration. `/auto on|off` is its single authority and the
`/config` display reads that live state. The retired `watcher.enabled` leaf is a warning-only tombstone;
loading it cannot arm or disarm the watcher and `/config set watcher.enabled` is refused.

## Framing notes and architecture-fork support (`framing_notes.*` / `ace.fork_mpr.*`)

| Key | Default | Meaning |
|---|---|---|
| `framing_notes.enabled` | `false` | Optional non-gating framing-note capture tool. `record_constraints` writes `notes/framing.md`; those notes are capture-only and are not auto-injected into coder handovers. Off ⇒ byte-identical |
| `constraint_gate.enabled` | alias | **Deprecated one-release alias** mapped strictly to `framing_notes.enabled`, then removed from the effective tree. Runtime sets through the alias update the canonical key and warn. |
| `safety.constraint_conflict_detect` | retired | **Deprecated tombstone.** Any legacy value warns, is ignored, and is stripped; `/config set` refuses it. There is no replacement network hard-check. |
| `design_gate.enabled` | retired | **Deprecated tombstone.** Loading either `true` or `false` emits one warning and ignores the value. `/config set` refuses it. The design proposal→decision lifecycle, implementation approval check, language anti-drift, and approved-standard injection are always on. |
| `advance_gate.enabled` | retired | **Deprecated tombstone.** Loading either legacy value emits one warning and ignores it; `/config set` refuses it. Completion authority is always on: only readable, non-empty feedback with normalized `status: done` may advance a task. |
| `ace.fork_mpr.enabled` | `false` | Architecture-fork worker at a recognized fork (`/fork` recommendation fill + decide→learn). The retired constraint-envelope leg is gone. Off ⇒ no worker, no learn |

`process.enabled` is likewise a one-release deprecated alias mapped strictly to
`process.hints_enabled`. The retired `lessons.enabled` and `lessons.max_per_scope` leaves are warning-only
tombstones; they cannot disable ACE or resize its playbook. Use `ace.max_bullets` for the latter.

ACE learned-state safety is not configurable. The retired `ace.safe_promote` key is a warning-only
tombstone: loading it ignores and strips the value, and `/config set` refuses it. Every online adaptation
snapshots the active playbook and adapts a deep candidate. With a deployment-injected evaluator, only a
measured non-regression promotes; regressions and unavailable scores quarantine the candidate without
changing active state. Without an evaluator, the default structural gate promotes unless the candidate
empties a non-empty playbook or loses more than 50% of its bullets, so both success- and failure-learning
land while catastrophic loss is quarantined and the snapshot remains the rollback net.

Egress enforcement is not configurable. The retired `security.egress_analysis.enabled` key and
`GX10_EGRESS_ANALYSIS_ENABLED` env alias are warning-only tombstones; loading either is ignored and
`/config set` refuses the key. Posture belongs only to the approved design's `## Build policy` as
`network: none|declared|open`. No build-policy section advances without analysis; a present section with a
missing/invalid posture, or a restrictive-posture analyzer failure, refuses advance.

Boolean leaves take a **real** boolean, not Python truthiness. In a **config file or merged dict** a boolean
leaf must be an actual JSON `true` / `false`; a string (`"true"` / `"false"`) or an int (`0` / `1`) is
**refused** by the typed schema (`_as_bool` raises), so `"false"` can never read as truthy nor `1` as false.
The **environment** boundary and the `/config set` word form are the only places that accept the documented
spellings — `true` / `false` / `1` / `0` / `yes` / `no` / `on` / `off` (case-insensitive) — parsing them to a
real bool before validation; any other spelling there is warned and ignored, never silently truthy.

## Tooling envelope (`security.tooling_envelope.*`)

Every coder-spawn path authorizes the post-override executable and command-template shape immediately
before spawn and refuses fail-closed on a mismatch. Authorization has no disable path.

| Key | Default | Meaning |
|---|---|---|
| `security.tooling_envelope.enabled` | retired | Deprecated tombstone; values warn and are ignored because authorization is always on |
| `security.tooling_envelope.allow_list` | derived | Boot-only entries shaped as `{bin, cmd_template}`; omitted derives enabled CLI launch tuples, while explicit `[]` denies all external spawns |

The exported schema is generic. Concrete binary names, globs, and command templates belong in deployment
config, not in public core. Matching uses executable identity (real path when available, otherwise basename)
plus a normalized command-template shape; a path-shaped allow-list entry pins the exact realpath while bare
command names may match case-insensitively against the candidate executable stem after stripping a trailing
`.exe`, `.cmd`, `.bat`, `.com`, or `.ps1`. Portable expansion is deliberately narrow and shared with the
TypeScript client: `$VAR`/`${VAR}` and a leading bare `~` are expanded, undefined environment references
stay literal, and only `*`/`?` glob wildcards are recognized. Bracket classes, `%VAR%`, and `~user` are
literal. Malformed inputs and an empty derived set refuse fail-closed. The server always ships the
non-secret effective allow-list on `/pending` so the TypeScript client and standalone Python client can
apply the same local-spawn guard before running a handover. A refused CLI is not spawned. The provider
dispatcher may still use its engine-owned in-process reasoning fallback; that
fallback performs model calls through the existing client and is not an external coder launch. The Claude
autopilot path authorizes only the two
canonical argv shapes (default non-stream and stream-json); model, effort, and prompt are normalized as
variable slots and extra flags still refuse.

## Model command isolation and result fencing (`security.*`)

| Key | Env | Default | Meaning |
|---|---|---|---|
| `security.sandbox` | `GX10_SANDBOX` | `auto` | Mandatory model `execute_command` backend policy: `auto`, `bwrap`, or `firejail`. Invalid values are refused. Legacy `off`/`none` warns and is ignored; it cannot authorize unsandboxed execution. Linux without the selected backend and all Windows hosts refuse model commands fail-closed. |
| `security.injection_defense` | `GX10_INJECTION_DEFENSE` | retired | Deprecated tombstone. Values warn and are ignored, and `/config set` refuses the key because untrusted-result fencing is always on. |

`/config set security.sandbox` accepts only the three live policy values. The production Linux execution host
must install `bwrap` (preferred by `auto`) or `firejail`. This policy governs native and versioned bridged
model-command lanes; it does not convert the explicit Ink `/sh` operator channel into a model tool.

Injection fencing has no tuning switch. Character-capped reads and structured/already-budgeted web,
provider, memory, MPR, and plugin results all cross one post-serialization fence; structured results skip the
destructive character cap. A fence wrapper failure withholds raw content and returns a safe error.

## Provider router (`providers.*`)

The provider router/dispatcher is **off by default** (`server` setup); it is enabled in the `local` setup.
`setup.type` is the single topology authority. The retired `providers.enabled` key and `GX10_PROVIDERS` env
variable warn and are ignored; neither adds a second condition to dispatcher enablement.
The private deployment supplies the real provider pool (models, $/token, endpoints) in its own `conf/` —
core ships no provider literals.

| Key | Env | Default | Meaning |
|---|---|---|---|
| `providers.default_id` | `GX10_PROVIDERS_DEFAULT` | `None` | default provider id |
| `providers.max_agents` | `GX10_PROVIDERS_MAX_AGENTS` | `3` | server CLI-pool cap (not the client `--max-agents`) |
| `providers.cli_timeout_s` | `GX10_PROVIDERS_CLI_TIMEOUT_S` | `900` | positive finite CLI timeout, maximum `3600` seconds |
| `providers.budget.usd_cap` | `GX10_PROVIDERS_BUDGET_USD` | `None` | per-run USD budget cap |
| `providers.pool` | — | `[]` | provider specs; filled by the deployment `conf/`, never hard-coded in core |
| `providers.effort_max_tokens` | — | `{low: 512, medium: 1024, high: 2048, xhigh: 4096}` | per-effort output-token cap used by routing / cost scoring |

> `providers.scoring.*` is a warning-only tombstone until a real live policy is implemented. The router
> honestly applies fixed built-in scoring constants; file values are stripped and runtime sets are refused.

## Scope & persistence

- The override lives in the **running process only** — it is **not** written back to any file. A restart
  reloads from defaults/file/env/flags. Persist a value by putting it in the config file or an env var.
- Single-process control surface: there is no auth layer here beyond the server's own trust profile
  (see `docs/…` security). Treat it like any other orchestrator command.

## For plugin authors

Expose a runtime toggle by simply **reading your section from `_EFFECTIVE_CFG` per call** and documenting
the keys. No core change is needed — `/config set my_plugin.some_flag on` just works. A robust pattern is
to **decouple a load-gate from a runtime-gate**: an env var decides whether the plugin's tool is
registered at all (off = the engine stays byte-identical), while a `my_plugin.enabled` config key decides
whether the loaded tool is active — toggled live via `/config set my_plugin.enabled on`.
> **Mandatory staging protections.** Task validation, required handover verification, the
> output-quality breaker, and no-guessing ambiguity detection cannot be disabled. The former switches
> `ack.enabled`, `verify.enabled`, `quality.enabled`, and `safety.ambiguity_detect` are warning-only
> tombstones: file values are ignored and runtime writes are refused. `GX10_AMBIGUITY_DETECT` is likewise
> ignored with a deprecation warning. Operational tuning remains available through
> `verify.grounding_threshold`, `quality.threshold`, `quality.min_consecutive`, and `quality.window`.

> **Finite failure and heartbeat protections.** Failure classification, per-task attempt
> accounting, terminal retry-budget escalation, and the progress heartbeat cannot be disabled.
> `strategy.enabled` is a warning-only tombstone. `strategy.budget` and
> `heartbeat.stall_seconds` and `heartbeat.claim_lease_seconds` remain bounded positive tuning; the progress
> heartbeat deliberately does not infer a stall for a task that has never produced a coder-log or feedback
> signal, and the lease reconciler skips every task without a client claim stamp.
