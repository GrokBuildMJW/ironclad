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

1. `set` writes the (coerced) value into `_EFFECTIVE_CFG` at the dotted path.
2. It then calls `_apply_config(_EFFECTIVE_CFG)` to **re-derive the engine globals** from the full tree
   (idempotent; reads the merged tree as the source of truth).
3. `_apply_config` re-derives **all** core globals from the whole tree and simply **ignores** sections it
   does not model — so setting a **plugin** key normally succeeds quietly (the plugin re-reads its own
   slice of `_EFFECTIVE_CFG` on its next use). The `stored (not a core global: …)` note is a defensive
   fallback shown **only if `_apply_config` raises** (e.g. the live tree is missing a section core expects);
   the dotted write itself still stands.
4. With no live config yet (`_EFFECTIVE_CFG is None`, i.e. before the server has merged its config) `set`
   is a friendly no-op.

## Frozen (boot-only) keys

Some keys wire something at **startup** that a later write cannot re-thread. Currently frozen
(`_FROZEN_CONFIG_KEYS`): **`setup.type`** (selects the offload runner — see [`setup-types.md`](setup-types.md)),
**`security.profile`** (builds the trust policy + the effective bind host, e.g. `sealed`→loopback — see
[`security.md`](security.md)), **`security.web_in_sealed`** (the sealed-profile web-search opt-in — a runtime
write must not lift the seal without a restart), and **`search.enabled` / `search.adapter` /
`search.api_key_env`** (the web-search seam is boot-wired; re-pointing the adapter or its key at runtime
would not re-thread it). Mutating a frozen key at runtime would be incoherent, so `/config get <key>` still
reads it but `/config set <key> …` is **refused** with a clear message ("boot-only — set it in the deploy").
The frozen set lives in core, generic and extensible. Change a frozen key in the config file / env and restart.

> **When does an override take effect?** Core globals: immediately (step 2). Plugin sections: on their
> next read of `_EFFECTIVE_CFG` (most plugins re-read per request, so effectively the next call).

## Web search (`search.*`)

The `web_search` tool is configured under the `search.*` block; the corresponding `GX10_SEARCH_*`
env vars override it (non-secret knobs only).

| Key | Env | Default | Meaning |
|---|---|---|---|
| `search.enabled` | `GX10_SEARCH_ENABLED` | `true` | master on/off (frozen, boot-only) |
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

## Loop-intelligence toggles (`lessons.*` / `quality.*` / `process.*` / `loop_profiles`)

The reflection-layer seams are **opt-in and default OFF** — each is a byte-identical no-op until you
enable it with `/config set …`. They have **no env override** (set them in the config file or at runtime).
See [`status.md`](status.md) for the honest wiring status of each, and [`lesson-api.md`](lesson-api.md)
for the lesson provider API.

| Key | Default | Meaning |
|---|---|---|
| `lessons.enabled` | `false` | register the project-private lesson distiller (`EngineLessonStore`); off ⇒ the lesson seam is a no-op |
| `lessons.max_per_scope` | `200` | per-scope compaction cap (oldest lessons dropped first) |
| `quality.enabled` | `false` | build the per-task output-quality circuit breaker; off ⇒ no breaker |
| `quality.threshold` | `0.5` | a mark-only verifier score below this counts as a low sample |
| `quality.min_consecutive` | `3` | consecutive low samples that trip the (advisory) breaker |
| `quality.window` | `20` | rolling number of scores retained |
| `process.enabled` | `false` | record typed process-lessons at completion + inject a pre-turn hint (also needs `lessons.enabled`) |
| `process.max_hints` | `3` | max working-approach hints folded into the pre-turn prefix |
| `loop_profiles.default` | `{}` | per-run loop-budget overrides (`max_iterations` / `retry_budget` / `effort`); empty ⇒ the engine globals apply (the live chat-loop bound) |
| `loop_profiles.by_type` | `{}` | per-`TaskType` overrides, e.g. `{"research": {"max_iterations": 40}}` — **reserved** (resolved but not yet consumed by a per-type loop) |

## Constraint compliance toggles (`constraint_gate.*` / `safety.*` / `ace.fork_mpr.*`)

| Key | Default | Meaning |
|---|---|---|
| `constraint_gate.enabled` | `false` | Optional non-gating framing-note capture tool. `record_constraints` writes `notes/framing.md`; when `design_gate.enabled` is on those notes are capture-only and are not auto-injected into coder handovers. Off ⇒ byte-identical |
| `safety.constraint_conflict_detect` | `false` | Retired/config-compatible no-op kept for older config files. Product constraint-conflict detection, product constraint forks, and constraint hard-checks are removed. |
| `design_gate.enabled` | `false` | Design-variant lifecycle + implementation approval gate. When on, `record_design` uses retained proposal variants, `/design --options [N]` requests 2..8 trade-off variants (default 2) and warns if fewer proposal files are recorded, `/approve design [<id>]` promotes one approved decision, build-boundary anti-drift at impl `stage_handover` / `plan_units` compares task typed `language` to the approved design standard, and coder handovers inject the approved standard plus `## Build policy`. Off ⇒ byte-identical legacy single-doc design flow |
| `ace.fork_mpr.enabled` | `false` | M5 architecture-fork worker at a recognized fork (`/fork` recommendation fill + decide→learn). The retired constraint-envelope leg is gone. Off ⇒ no worker, no learn |

Gate/feature flags use **strict** boolean coercion (`_as_bool`): only JSON `true` or the true strings
`true` / `1` / `yes` / `on` enable a flag; `"false"` / garbage fail soft to off.

## Tooling envelope (`security.tooling_envelope.*`)

ADR-0007 adds a default-off policy surface for authorizing coder CLI substrate commands. When enabled,
every coder-spawn path authorizes the post-override executable and command-template shape immediately
before spawn and refuses fail-closed on a mismatch.

| Key | Default | Meaning |
|---|---|---|
| `security.tooling_envelope.enabled` | `false` | opt-in master switch; off means existing coder/provider behavior is byte-identical |
| `security.tooling_envelope.allow_list` | `[]` | entries shaped as `{bin, cmd_template}` for authorized coder CLI commands |

The exported schema is generic. Concrete binary names, globs, and command templates belong in deployment
config, not in public core. Matching uses executable identity (real path when available, otherwise basename)
plus a normalized command-template shape; a path-shaped allow-list entry pins the exact realpath while bare
command names may match by basename. Portable expansion is deliberately narrow and shared with the
TypeScript client: `$VAR`/`${VAR}` and a leading bare `~` are expanded, undefined environment references
stay literal, and only `*`/`?` glob wildcards are recognized. Bracket classes, `%VAR%`, and `~user` are
literal. Malformed inputs refuse fail-closed when the policy is enabled. The server also ships the
non-secret effective allow-list on `/pending` so the TypeScript client and standalone Python client can
apply the same local-spawn guard before running a handover. A refused CLI is terminal for that route: it is
not silently spilled to an in-engine/API substrate. The Claude autopilot path authorizes only the two
canonical argv shapes (default non-stream and stream-json); model, effort, and prompt are normalized as
variable slots and extra flags still refuse.

## Provider router (`providers.*`)

The provider router/dispatcher is **off by default** (`server` setup); it is enabled in the `local` setup.
The private deployment supplies the real provider pool (models, $/token, endpoints) in its own `conf/` —
core ships no provider literals.

| Key | Env | Default | Meaning |
|---|---|---|---|
| `providers.enabled` | `GX10_PROVIDERS` | `false` | global on/off; off ⇒ the dispatcher delegates to in-engine fan-out (byte-identical) |
| `providers.default_id` | `GX10_PROVIDERS_DEFAULT` | `None` | default provider id |
| `providers.max_agents` | `GX10_PROVIDERS_MAX_AGENTS` | `3` | server CLI-pool cap (not the client `--max-agents`) |
| `providers.cli_timeout_s` | `GX10_PROVIDERS_CLI_TIMEOUT_S` | `None` | timeout for the default CLI runner (`None` ⇒ no timeout) |
| `providers.budget.usd_cap` | `GX10_PROVIDERS_BUDGET_USD` | `None` | per-run USD budget cap |
| `providers.pool` | — | `[]` | provider specs; filled by the deployment `conf/`, never hard-coded in core |
| `providers.effort_max_tokens` | — | `{low: 512, medium: 1024, high: 2048, xhigh: 4096}` | per-effort output-token cap used by routing / cost scoring |

> `providers.scoring` (router scoring weights) also exists in the tree, but the router currently applies
> **fixed built-in values** for it — treat it as reserved until it is wired to read the config.

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
