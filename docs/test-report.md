# Test report

> Maximum transparency: this is the actual state of testing, including issues found
> **and fixed** during the campaign. Counts re-confirmed **2026-06-22** (offline suite; live
> verification 2026-06-17). For per-component wiring status see
> [`status.md`](status.md); for what's planned see [`roadmap.md`](roadmap.md).

## Summary

| | |
|---|---|
| Automated tests (offline, no model) | **1691 passed** |
| Live smoke tests (skipped without a model) | **9** |
| **Total Python** | **1700** |
| TypeScript client tests (`node:test`) | **360 passed** (364 total, 4 skipped) |
| Full agentic loop, end to end, with a **real** code-agent | **verified** |
| Issues found during the campaign | **1 functional gap + 5 review findings — all found and fixed** (see below) |

All offline tests run with no network and no model (the OpenAI client and heavy deps are
stubbed), so they are deterministic and fast (~24 s). The live suite is **skipped by
default** and only runs when pointed at a real server.

## How to reproduce

```bash
# 1) offline suite — deterministic, no model needed
pytest -q                                   # from core/  → 1691 passed, 9 skipped

# 2) live smoke — against your own running orchestrator
GX10_LIVE_URL=http://<your-host>:8100 pytest -k live -q     # 9 passed
# (set GX10_LIVE_TOKEN too for the token/sealed profiles)
```

## Coverage by area

The breakdown below groups the suite by capability area and sums to
the **1700** total (1691 offline + 9 live). It is a high-level view of internal QA
coverage; the granular test names and the maintainers' internal tracker are
intentionally not enumerated here.

| Area | Tests |
|------|-------|
| **Agent-Contract-Kernel** — schema SSOT, validate→reask, constrained emission, capability registry | 87 |
| **Function-calling robustness** — tool-argument validation and model-agnostic call recovery | 24 |
| **Server / client split & security** — HTTP surface, trust profiles, sessions, sealing, the config tree + runtime config, command router, doctor, catalogue endpoint, the server-side tool bridge, and the coders / health observability blocks | 117 |
| **Provider-router / dispatch** — backend registry, routing policy, artifact routing, spill / fallback, setup-type resolution, reviewer anti-affinity, and first-class web-search routing | 89 |
| **Web search & current-info routing** — the web-search tool gating + handler, the current-info intent classifier (English + German), the strict input contract + domain normalizer, the standalone adapter seam + a native HTTP adapter, the model-facing Sources formatter, the web_search prompt + tool-description, the sealed trust gate, the config + secret surface, the search-progress renderer, the 16-test spec consolidation, the tool-as-shell guard, and a fail-closed shell guardrail | 147 |
| **Memory & context** — Mem0 client, chunking, RAG, the rolling summary, bounded summarizer input, deep query, vault reconcile, the warm tier, and the token-budgeted handover brief | 98 |
| **Read-only Memory MCP** — a dependency-free stdio JSON-RPC server exposing project memory as read-only search + deep-query tools, with a sealed-gated launch | 10 |
| **Open plugin surface** — discover and expose `skills/*` plugins with no core patch | 7 |
| **Extension SDK** — the curated public `ack.sdk` surface (contract, re-export identity, gate / schema / assemble) | 7 |
| **Packaged-plugin loading** — the `ironclad.plugins` entry point (root resolution, additive load, fail-soft) | 10 |
| **Export-leak guard** — internal artifacts kept out of the boundary and the public export | 4 |
| **Example plugin** — the shipped separate-repo example: discovers and runs via the loader and passes its own gate | 6 |
| **Playbook skill kind** — `SKILL.md` parse / validate / discover, progressive disclosure | 15 |
| **Skill generator** — spec → scaffold both skill kinds, schema-valid by construction | 7 |
| **Skill library catalogue** — manifest index, semver, provenance, install / update | 6 |
| **Skill registration gate** — doctor-preflight / schema-check / eval-gate, no unchecked code | 12 |
| **Skill lifecycle end-to-end** — generate → gate → register → load → invoke | 3 |
| **Shared content i18n** — the `ack.i18n` overlay loader (parameterized dir, fallback) | 6 |
| **Core built-in loader** — always-on built-ins from a fixed dir; plugins additive | 4 |
| **MPR core built-in** — router / registry / synthesis / audit / panels / templates / eval / packaging | 381 |
| **Prompt-library item** — `kind: prompt` parse / validate / discover + variable build | 7 |
| **Multilingual prompt assembly** — template + values → target language | 6 |
| **Prompt slash surface & elicitation** — list → guided ask-next → assemble + language | 9 |
| **Curated prompt library** — the shipped starter prompts: discover + gate + assemble (English + German) | 24 |
| **Discovery commands** — `/prompts` and `/skills` list the one loaded registry | 6 |
| **Per-item prompt invocation** — `/<prompt-name>` resolve + parse + elicit / assemble | 14 |
| **Orchestration state** — task lifecycle / dedup, initiative, autoplan, state end-to-end | 45 |
| **Parallelism** — governed fan-out, the in-engine tool, single-writer reduce, the parallel router | 29 |
| **Thin client + BYO code-agent** — the agent pool, a configurable agent-command template, managed transport, the config-driven code-agent registry, a per-agent boot probe, result classification, and onboarded-but-disabled agents | 85 |
| **Runtime-aware output & language** — encoding safety, color gating, reply language | 14 |
| **Token budget / context trimming** — token-accurate budgeting, a pre-flight overflow guard with emergency trim, and live context-length discovery | 56 |
| **Misc** — manual cat tool, orchestrator version | 7 |
| **Demo vessel** — the example-workspace doctor preflight | 1 |
| **Documentation & release integrity (internal QA)** — documentation-reality checks, the generated roadmap and test counts, export-sync verification, the clean-room pre-publish proof, deploy-consistency checks, and the maintainers' release-process guards | 348 |
| **Live smoke** — real model, all endpoints | 9 |

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
   validated through the ACK contract gate) → a task is created and the handover staged.
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
