# Test report

> Maximum transparency: this is the actual state of testing, including issues found
> **and fixed** during the campaign. Counts re-confirmed **2026-06-22** (offline suite; live
> verification 2026-06-17). For per-component wiring status see
> [`status.md`](status.md); for what's planned see [`roadmap.md`](roadmap.md).

## Summary

| | |
|---|---|
| Automated tests (offline, no model) | **1085 passed** |
| Live smoke tests (skipped without a model) | **9** |
| **Total Python** | **1094** |
| TypeScript client tests (`node:test`) | **340 passed** (344 total, 4 skipped) |
| Full agentic loop, end to end, with a **real** code-agent | **verified** |
| Issues found during the campaign | **1 functional gap + 5 review findings — all found and fixed** (see below) |

All offline tests run with no network and no model (the OpenAI client and heavy deps are
stubbed), so they are deterministic and fast (~24 s). The live suite is **skipped by
default** and only runs when pointed at a real server.

## How to reproduce

```bash
# 1) offline suite — deterministic, no model needed
pytest -q                                   # from core/  → 1085 passed, 9 skipped

# 2) live smoke — against your own running orchestrator
GX10_LIVE_URL=http://<your-host>:8100 pytest -k live -q     # 9 passed
# (set GX10_LIVE_TOKEN too for the token/sealed profiles)
```

## Coverage by area

Counts below are reproduced from `pytest --collect-only` (2026-06-22) and sum to
the **1094** total (1085 offline + 9 live) — now includes the MPR core built-in suite.

| Area | Test files | Tests |
|------|-----------|-------|
| **Agent-Contract-Kernel** (schema SSOT, validate→reask, constrained emission, registry) | `registry`, `case_spec`, `constrained_emission`, `validated_emit`, `engine_ack_gate`, `lodestar_tracking` | 87 |
| **Function-calling robustness** (tool-arg validate→reask, model-agnostic recovery) | `tool_args`, `tool_extract` | 24 |
| **Server / client split & security** (HTTP surface, trust profiles, sessions, sealing, config tree + runtime config, command router, doctor, catalogue endpoint, server-side tool bridge) | `server_split`, `security`, `config_tree`, `config_runtime`, `commands`, `doctor_endpoint`, `catalogue_endpoint`, `tool_bridge`, `session_persist` | 79 |
| **Provider-router / dispatch (P0)** (backend registry, routing policy, artifact routing, spill/fallback, setup-types) | `dispatch`, `router`, `providers`, `providers_config`, `artifact_routing`, `offload_topology` | 69 |
| **Memory & context** (Mem0 client, chunking, RAG, summary, deep query, vault reconcile, warm tier) | `memory`, `memory_chunking`, `worker_memory`, `context_rag`, `context_summary`, `deep_query`, `reconcile_vault`, `warm` | 78 |
| **Open plugin surface** (discover + expose `skills/*` plugins, no core patch) | `plugins` | 7 |
| **Extension SDK** (`ack.sdk` curated surface: `__all__` contract, re-export identity, gate/schema/assemble via SDK) | `sdk` | 7 |
| **Packaged-plugin loading** (`ironclad.plugins` entry-point: root resolution, additive load, fail-soft) | `entrypoint_loader` | 10 |
| **Export-leak guard** (internal plugin repo forbidden in boundary+export; synthetic-leak flagged; real tree clean) | `export_leak_guard` | 4 |
| **Example plugin** (shipped separate-repo example: discovers+runs via loader, schema matches SDK, passes its own `ack.sdk.gate` via a sibling test) | `example_plugin`, `reverse` | 6 |
| **Playbook skill kind** (SKILL.md parse/validate/discover, progressive disclosure, `use_skill`) | `playbook` | 15 |
| **Skill generator** (spec → scaffold both kinds, schema-valid by construction) | `skillgen` | 7 |
| **Skill library catalogue** (manifest index, semver, provenance, install/update) | `catalogue` | 6 |
| **Skill registration gate** (tool doctor-preflight / playbook schema+check / prompt eval-gate, no unchecked code) | `gate` | 12 |
| **Skill lifecycle end-to-end** (generate → gate → register → load → invoke, both kinds) | `skill_e2e` | 3 |
| **Shared content i18n** (`ack.i18n` overlay loader, parameterized dir, fallback) | `i18n` | 6 |
| **Core built-in loader** (always-on built-ins from a fixed dir; plugins additive) | `builtin_loader` | 4 |
| **MPR core built-in** (router/registry/synthesis/audit/panels/templates/eval/packaging) | `skills/mpr/tests/*` | 381 |
| **Prompt-library item** (`kind: prompt` parse/validate/discover + variable build) | `prompt` | 7 |
| **Multilingual prompt assembly** (template + values → target language via ack.i18n) | `promptgen` | 6 |
| **Prompt slash surface & elicitation** (`use_prompt` list → guided ask-next → assemble + lang) | `prompt_cmd` | 9 |
| **Curated prompt library** (7 shipped starter prompts discover + gate + assemble EN/DE, drop-MD adds) | `prompt_library` | 24 |
| **Discovery commands** (`/prompts`, `/skills` list the one loaded registry) | `discovery_cmds` | 6 |
| **Per-item prompt invocation** (`/<prompt-name>` resolve + parse + elicit/assemble) | `prompt_invocation` | 14 |
| **Orchestration state** (TaskStore lifecycle/dedup, initiative, autoplan, state e2e) | `taskstore`, `initiative`, `autoplan`, `state_e2e` | 45 |
| **Parallelism** (governed fan-out, in-engine tool, single-writer reduce, parallel router) | `workers`, `parallel_tool`, `worker_reduce`, `parallel_router` | 29 |
| **Thin client + BYO code-agent** (agent pool, `GX10_AGENT_CMD` template, managed transport) | `client_pool`, `client_transport` | 14 |
| **Runtime-aware output & language** (encoding safety, color gating, reply language) | `output`, `language` | 14 |
| **Token budget / context trimming** | `token_budget` | 8 |
| **Misc** (manual cat tool, orchestrator version) | `manual_cat`, `version` | 7 |
| **Demo vessel** (example workspace doctor preflight) | `demo_vessel` | 1 |
| **Docs & process** (doc-reality-audit + roadmap generator + process-doctor invariants + export-sync + test-count generator, negative tests) | `doc_audit`, `gen_roadmap`, `process_doctor`, `export_sync_check`, `export_secret_gate`, `release_preflight`, `gen_test_counts`, `required_checks`, `deploy_consistency` | 96 |
| **Live smoke** (real model, all endpoints) | `live_smoke` | 9 |

## Live end-to-end verification

Run against a real deployment — a DGX Spark (GB10) serving **Qwen3.6-35B-A3B-NVFP4** via
vLLM with the orchestrator and Mem0 memory stack co-located, driven from a workstation
over the LAN.

**Orchestrator HTTP surface (live smoke, 9/9):** health, a simple chat turn, a
**tool-using** turn (the model calls `list_directory` and answers from it), streaming,
the task snapshot, governed fan-out (concurrent, measured speedup), input validation
(`/fanout` rejects empty), cancel, and a memory-backed turn (`query_memory`).

**The full agentic loop — the headline flow — end to end:**

1. A chat turn makes the orchestrator plan and `stage_handover` a task (its `task_json`
   validated through the ACK contract gate) → task **KGC-1** created, handover staged.
2. The thin client pulls `/pending`, runs the local code-agent against a local working
   copy, and uploads the result via `/feedback`.
3. The server's reconciler advances the task → **done**.

This was verified **twice**: once with a deterministic stub agent (proves the
client↔server↔reconciler contract repeatably), and once with a **real `claude --print`
code-agent** that actually created the file in the local repo, wrote its feedback, and
drove the task to **done** — proving the advertised "code stays on your machine, the
code-agent runs there" flow with the real binary.

**Sealed channel (Phase d):** separately verified over a real SSH tunnel — loopback-only
bind, gated routes refuse without a session, a real model turn through the sealed channel,
re-seal on disconnect (details in [`status.md`](status.md)).

## Issues found during this campaign — and fixed

Transparency over polish: the full test deliberately exercised the real path, and it
caught a real gap.

- **Headless code-agent could not write files.** The thin client launched `claude
  --print` with **no permission mode**, so the local code-agent had no way to approve
  file writes in headless operation — it exited having done nothing. The orchestration
  plumbing was fine, but the advertised "edit local code" flow was broken for the real
  binary. **Fixed:** the client now passes `--permission-mode` (default `acceptEdits`,
  configurable via `GX10_CLAUDE_PERMISSION_MODE`); re-verified end to end with a real
  code-agent. A regression test pins the flag.

Earlier in the same hardening pass, a smaller gap was also fixed: `/tasks` was readable
without the deployment secret under the auth profiles (now gated), and a flaky
socket-timing assertion in the tunnel test was made deterministic.

**Adversarial review of the new code.** Two independent reviewers audited the full diff
of this work and surfaced a handful of real issues, all fixed and regression-tested:

- **(high) Model-agnostic tool-call recovery could hijack a JSON answer.** The recovery
  path that reads tool calls from *text* (for models without native tool-calls) had a
  branch that fired on any bare top-level JSON object whose `name` matched a tool — so a
  legitimate answer that happened to be JSON (or an echoed tool spec) could be silently
  re-interpreted into a **destructive** call (`write_file`/`execute_command`/`delete_file`).
  Fixed: only **explicit** `<tool_call>` tags and fenced blocks are recovered now; a bare
  object is never treated as a call.
- **(med) Auth-gate / router path mismatch.** The gate and the router compared the raw
  path; a query string could desync them. Both now normalize to the query-free path.
- **(med) Request-body cap.** `Content-Length` is now capped (8 MiB) to bound per-
  connection allocation on the threaded server.
- **(med) Orphaned tunnel child.** If the client-managed tunnel failed to come up, its
  subprocess was not torn down (the context manager's exit doesn't run when entry
  raises). It is now reaped on any failure.
- **(med) Config-tree slurp.** Directory config descent now skips hidden/dotted subdirs
  (no `.git`/`.vscode` pickup).

## Honest limitations

- The live suite needs a running endpoint; it is **skipped** in plain CI.
- The real code-agent step depends on a local `claude` binary and a permission mode that
  allows unattended edits (`acceptEdits`); for tasks that also run commands, set
  `bypassPermissions` — understand the implication (it runs on your own machine, against
  your own code, session-gated).
- Multi-user identity/authorization is **not** built (single-tenant by design — see
  [`roadmap.md`](roadmap.md)); nothing here tests multi-tenant isolation because there
  is none yet.
- `main` is a development snapshot. These results reflect that snapshot, not a release.
