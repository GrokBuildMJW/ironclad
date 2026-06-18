# Changelog

All notable changes to Ironclad are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/); the project is **pre-release** (0.0.x).
Released versions are listed below; upcoming work accumulates under *Unreleased*.

> Docs are code: a change does not ship without a Changelog entry (the promote gate
> enforces a non-empty *Unreleased* section).

## [Unreleased]

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
