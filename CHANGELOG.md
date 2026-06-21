# Changelog

All notable changes to Ironclad are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/); the project is **pre-release** (0.0.x).
Released versions are listed below; upcoming work accumulates under *Unreleased*.

> Docs are code: a change does not ship without a Changelog entry (the promote gate
> enforces a non-empty *Unreleased* section).

## [Unreleased]

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
