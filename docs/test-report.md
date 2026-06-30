# Test report

> Maximum transparency: this is the actual state of testing, including issues found
> **and fixed** during the campaign. Counts re-confirmed **2026-06-30** (offline suite; live
> verification 2026-06-17). For per-component wiring status see
> [`status.md`](status.md); for what's planned see [`roadmap.md`](roadmap.md).

## Summary

| | |
|---|---|
| Automated tests (offline, no model) | **2207 passed** |
| Live smoke tests (skipped without a model) | **9** |
| **Total Python** | **2216** |
| TypeScript client tests (`node:test`) | **355 passed** (359 total, 4 skipped) |
| Full agentic loop, end to end, with a **real** code-agent | **verified** |
| Issues found during the campaign | **1 functional gap + 5 review findings — all found and fixed** (see below) |

All offline tests run with no network and no model (the OpenAI client and heavy deps are
stubbed), so they are deterministic and fast (~24 s). The live suite is **skipped by
default** and only runs when pointed at a real server.

## How to reproduce

```bash
# 1) offline suite — deterministic, no model needed
pytest -q                                   # from core/  → 2207 passed, 9 skipped

# 2) live smoke — against your own running orchestrator
GX10_LIVE_URL=http://<your-host>:8100 pytest -k live -q     # 9 passed
# (set GX10_LIVE_TOKEN too for the token/sealed profiles)
```

## Coverage by area

The breakdown below groups the suite by capability area and sums to
the **2216** total (2207 offline + 9 live). It is a high-level view of internal QA
coverage; the granular test names and the maintainers' internal tracker are
intentionally not enumerated here.

| Area | Tests |
|------|-------|
| **Agent-Contract-Kernel** — schema SSOT, validate→reask, constrained emission, capability registry | 89 |
| **Failure classification** — the shared, string-valued `FailureClass` taxonomy SSOT + the deterministic, rule-based validated-emit classifier (the additive, advisory `ValidatedEmitResult.failure_class`; byte-identical no-op on success) + the providers→enum bridge re-mapping the 3-class run taxonomy onto one enum + the absolute never-raises backstop (epic #602 S602-3), plus the **code-agent-failover production** (#602 2.4/#805): `gx10._record_failure_class` classifies a failed run into the shared FailureClass at the server feedback path and surfaces it (`gx10._last_failure_class()` + a `failure_class` response field) for the Strategy consumer (2.5), opt-in per `strategy.enabled` (default-off byte-identical) | 31 |
| **Lesson distiller (provider)** — the project-private `EngineLessonStore` `ack.lessons` provider (epic #602 S602-5): scope-isolated persistence + recency/query ranking + typed categories + compaction + scope-priority `brief` + `forget` purge + robustness (corrupt/missing/weird-scope), and the OPT-IN `lessons.enabled` engine wiring (registers/clears/live-resizes the store, default-off byte-identical, never clobbers a foreign provider; malformed/overflow-cap safety; empty-scope-guarded `by_category` + `brief`; fail-soft on invalid-UTF-8/hostile-dunder/hostile-str-scope+lesson/hostile-category/bad-base-dir/save-failure) | 42 |
| **Strategy revisor** — the pure `revise(failure_class, attempt, budget) → Strategy` policy SSOT (per-class action snapshot, totality, spent-budget escalation, no-raise), the `validated_emit` `strategist` re-ask seam (byte-identical without it, hint appended with it, strategist-error swallowed), and the engine `providers.code_agent_strategy` application (epic #602 S602-7), plus the **engine failover consumer** (#602 2.5/#806): `gx10._revise_on_failure` runs `code_agent_strategy` per task on the `/feedback` failure path and surfaces a HUMAN_ESCALATION when the attempt budget is spent (a success resets the counter), opt-in per `strategy.enabled` (default-off byte-identical) | 22 |
| **Loop profiles** — the pure `resolve_loop_profile` per-TaskType deep-merge SSOT (default/by_type precedence, present-key override, retry-budget clamp, floors, enum-value key, never-raises) + the engine `_loop_profile` accessor driving the chat-loop bound (byte-identical default incl. a zero-iteration fallback, override pickup, run()-wiring guard) + per-profile `eval_verifiers` activation (8b) (epic #602 S602-8a/8b), plus the **per-TaskType failover budget** (#602 2.6/#807): `gx10._failover_budget` resolves `loop_profiles.by_type[<type>].retry_budget` (layered over `strategy.budget`, clamped to the hard ceiling) for the code-agent failover escalation — the first live `by_type` consumer on the dev-task pipeline | 26 |
| **Verifier / evaluation** — the mark-only `VerdictResult` + the three opt-in verifiers: `verify_rules` (deterministic predicates, raising-rule-is-a-fail), `verify_grounding` (injected retrieve, error→ungrounded, threshold), and the async budget-gated `verify_with_judge` (skips+uncharged when unaffordable, charges+runs when affordable, transport/parse/budget error→None + charge-only-on-completed-call: nothing-on-skip/transport/parse/non-verdict + charge-error-keeps-verdict), plus the frozen no-gate-field shape + never-raises on garbage rules/claims/hostile-name/hostile-len/hostile-strip (epic #602 S602-4), plus the **engine pre_handover Verifier-runner** (#602 2.1/#802): on the dev-task pipeline it runs behavioral rules over `task_json` + grounding of the handover claims via the cold store, stores a mark-only `VerdictResult` for the Quality breaker, registered/unregistered per `verify.enabled` (default-off byte-identical; a grounding-error stays fail-soft — a memory hiccup drops only grounding, the rules verdict survives) | 34 |
| **Quality circuit breaker** — the separate `QualityBreaker` trend logic (trips on min-consecutive sub-threshold scores, streak reset on recovery, at-threshold-not-low, clamp, window cap, frozen snapshot, fail-open-safe never-raises on garbage score/params) + the OPT-IN engine wiring (`_apply_quality_breaker` builds/clears/keeps-state, default-off no breaker, separate from the availability breaker) (epic #602 S602-9), plus the **post_handover consumer** (#602 2.7/#808): it feeds the mark-only Verifier score (`_last_verdict()`) into the breaker and surfaces a sustained-degradation trip — advisory, never gates — registered/unregistered per `quality.enabled` (default-off byte-identical) | 24 |
| **Process self-correction** — the pure `distill_process_lesson` (success→working-path, missing-clarification priority, none-when-not-actionable, never-raises) + `format_process_hint` (render/empty/limit/never-raises) + the OPT-IN engine wiring (`_record_process_lesson`/`_process_hint` roundtrip, byte-identical when disabled / no concrete provider / string-only provider / empty scope / overflow max_hints, never-raises on garbage) (epic #602 S602-6), plus the **`post_feedback` Hook-Bus re-home** (#602 2.2/#803): the Process-SC write is driven by `gx10._process_consumer_hook` through the real `_advance_pipeline` wrapper (one consistent reflection path, outside the vault lock) — it records via the concrete provider on a fresh completion, is a byte-identical no-op when off, does NOT double-record on an already-done re-advance, and stays fail-soft on a raising provider | 21 |
| **Loop-Intelligence Hook-Bus** — the standalone `ack.hooks` event bus (epic #602 SUB-2 / Teil-2 2.0, the keystone): `register_hook` (additive + idempotent, fail-loud on an unknown event / non-callable) + observer-only, fail-soft `dispatch` with an **O(1) byte-identical-default no-op**, copy-on-write + snapshot for the multi-threaded engine, and cancel/budget early-out; plus the engine publish-point wiring (`run()` fires `pre_turn`/`post_generate`/`post_toolresult`; the `_stage_handover`/`_advance_pipeline` wrappers fire `pre_handover`/`post_handover`/`pre_advance`/`post_feedback` outside the vault lock) — proven functional (the engine actually publishes) AND byte-identical with no hook registered, plus identity-based `unregister_hook` for clean opt-in deregistration without clobbering sibling hooks | 24 |
| **Closed-loop e2e** (#602 C2 done-gate) — the reflection loop proven LIVE end-to-end on the dev-task pipeline: a staged handover is scored (Verifier) → fed to the Quality breaker + trips → a run failure is classified (FailureClass) → the Strategy Revisor escalates on a spent budget; all-flags-off is a byte-identical no-op; plus the 8b `eval` per-type verifier selection (#602 / #809) | 3 |
| **Function-calling robustness** — tool-argument validation and model-agnostic call recovery | 24 |
| **Server / client split & security** — HTTP surface, trust profiles, sessions, sealing, the config tree + runtime config, command router, doctor, catalogue endpoint, the server-side tool bridge, the coders / health observability blocks, and the client<->commands.py command-parity guard | 121 |
| **Provider-router / dispatch** — backend registry, routing policy, artifact routing, spill / fallback, setup-type resolution, reviewer anti-affinity, first-class web-search routing, and the **handover effort auto-tiering by task class** (#500: security/architecture → xhigh, routine → high; explicit `effort:` wins; fail-open on an unmapped/unloadable class) | 102 |
| **Web search & current-info routing** — the web-search tool gating + handler, the current-info intent classifier (English + German), the strict input contract + domain normalizer, the standalone adapter seam + a native HTTP adapter, the model-facing Sources formatter, the web_search prompt + tool-description, the sealed trust gate, the config + secret surface, the search-progress renderer, the 16-test spec consolidation, the tool-as-shell guard, and a fail-closed shell guardrail | 147 |
| **Memory & context** — Mem0 client, chunking, RAG, the rolling summary, bounded summarizer input, deep query, vault reconcile, the warm tier, and the token-budgeted handover brief | 101 |
| **Lesson store / provider API** — the curated, versioned `ack.lessons` delegation seam (AD-10, #601 S14-3): a `runtime_checkable LessonProvider` (get_lessons / report_lesson / brief) + set/get_provider; fail-soft no-op when no provider is wired (reads `[]`, writes no-op, a provider error never breaks a turn); a scope-priority `brief()` merge (provider-or-composed, dedup + limit); and the **fail-closed redaction-gated `promote()`** (AD-9 — a project-private lesson is promoted to a broader scope ONLY through an approving redactor). The #602-unblocking surface; lesson semantics are the provider's | 20 |
| **Lesson seam wiring** — the engine integration of `ack.lessons` (AD-10, #601 S14-4): the handover read-site appends an advisory, scope-keyed lesson brief alongside the Memory brief, and the task-completion write-site reports the feedback as a scoped lesson (tagged with the task id) — both lazy-imported, both scoped to the active project/track `mem_scope`, and both byte-identical no-ops with no provider wired (the read returns nothing, the write does not even touch the feedback file), plus the **`post_feedback` Hook-Bus re-home** of the write site (#602 2.3/#804): the task-completion lesson write is driven by `gx10._lessons_consumer_hook` through the real `_advance_pipeline` wrapper (registered on **provider presence**, outside the vault lock), reporting on a fresh completion only (no double-report on an already-done re-advance), byte-identical no-op with no provider, fail-soft on a raising provider | 10 |
| **Scope-aware forget + scope tagging** — the substrate delete path (AD-10 / #601 S14-5): cold writes self-describe their origin `scope` in metadata (only when a project scope is bound — byte-identical for the base partition); `MemoryManager.forget(scope)` deletes a partition via the Mem0 `/delete_all` route (synchronous, fail-soft, fail-closed on an empty scope); the warm tier's `forget_scope(scope)` deletes the **exact**-scope session + retrieval-cache keys without cascading into deeper track scopes (fail-closed on an empty / glob-bearing scope); `ack.lessons.forget(scope)` is an optional, fail-soft provider verb; and `gx10._forget_scope` fans out across all three layers, fail-closed on an empty scope | 21 |
| **Memory-service scopes + orphan GC** — the partition-isolation guards + registry-keyed orphan garbage collection (AD-4 / #601 S15): the memory service's pure `require_scope` guard rejects an unscoped write/search (no `agent_id`) and refuses `run_id` as an isolation key; the engine's `MemoryManager.list_scopes()` lists the partitions present in the store (fail-soft), `_orphan_scopes` flags only **minted** `mem_ns` partitions with no registered project (never the base or a human-named scope, track sub-scopes judged by their project key), and `_reconcile_orphan_memory` forgets the orphans (dry-run by default, fail-soft per orphan; refuses to GC when the registry is unreadable), plus the **pure reflection-trigger policy** (`reflect_policy.reflect_decision`, #503/#767 MEMSVC-1): the threshold-fire decision consumes the write counter at fire time and suppresses a fire while a reflection is already running — so writes during a (slow) run accumulate toward the next cycle (no undercount) and no bail-thread is spawned on the busy lock (no churn); never raises on a bad counter/threshold, plus the **curated-global tier helpers** (#634 / AD-9): the pure `curate.promote_refusal` operator-gate (fail-closed — confirm + source scope + exactly one of redacted-text / source-query) and `merge_project_wins` (the project-wins fan-in: project results first + position-preserved, curated fills to the limit, dedup by text, provenance-tagged) | 30 |
| **Project track verbs** — the `/project track new|use|list` CLI (AD-2' / #601 S16): registry `add_track` (idempotent, fail-closed on an unsafe id / unknown project) + `set_active_track` (fail-closed when the track is unregistered), and the engine command that creates-and-switches (`new`) or switches (`use`) a track — rebinding the context so the vault subtree + memory sub-scope follow — or lists them, fail-closed without an active project | 12 |
| **Project mint (`/project new`)** — the guided-setup mint (#601 S16): the `new <name> [--type] [--path]` parser (name/type-lowercased/quoted-path/usage/unknown-type) and the mint pipeline that registers a fresh isolated project (root `<cwd>/<slug>` or `--path`, minted `mem_ns`, made active), binds the engine to it, and — with a `--type` — seeds the first vault unit via the initiative machinery; fail-closed on a duplicate root (registers before it creates the root dir, so no orphan) or an unknown type, and a name with no slug-able characters, and a bare/empty --type/--path flag; the mint activates through the real quiesced switch (conversation reset, no bleed) and requires a session | 17 |
| **Project delete / archive** — the registry-mediated lifecycle verbs (#601 S16): `set_archived` (registry); `/project delete <id> [--purge]` (forgets every track memory scope, removes the registry entry, leaves the dirs unless `--purge`, which is itself guarded against the cwd/boot/home/ancestors; deleting the ACTIVE project switches to default first; default never deletable); `/project archive|unarchive` (reversible flag, refuses the active/default project, hidden from `list` unless `--all`); and `_switch` refusing an archived target; plus the atomic `Registry.remove(expected_root=)` (no purge on a re-registered root) | 22 |
| **Export no-project-artifacts guard** — the AD-8 export invariant (#601 S17): the publish export carries NO runtime project state — `export_core.scan_project_artifacts` asserts no `.ironclad/` machinery, `.tracks/` vault subtrees, or `registry.json` reach the staged tree (a fail-closed export gate + a backstop to the copy-ignore patterns) | 5 |
| **Export test-drift guard** — the #845 lint (follow-up to #843): a fail-closed export gate (1d, `check_export_test_drift`) + suite test flagging any exported `test_*.py` that references the private `scripts/` tree without an absence guard (`.is_file()`/`.exists()`/`pytest.skip`) — such a test `FileNotFoundError`s in the public clean-room where `scripts/` is absent; covers the clean real tree, a synthetic unguarded reference, the is_file / skipif clears, and a no-reference no-op | 5 |
| **Self-dogfood isolation acceptance** — the AD-8 offline acceptance (#601 S17): drives the real `/project new -> /switch -> stage a unit -> switch back` cycle for two projects through the quiesced switch (no live infra / model / deliver) and asserts the whole-epic invariant — distinct non-base memory partitions, vault + state machinery under each project’s own root, work artifacts only under the active project, the base/default project untouched, and no cross-project conversation bleed | 5 |
| **Lifecycle DELIVER-leg gate** — the S13b wiring (#601 S13b / #632) of the S13a evidence primitives into a functioning gate: the pure `lifecycle_projector` maps dev-process **ledger** transitions to lifecycle stages (a green composed-gate leg → `tests`, a review-evidence leg → `reviews`, a `delivered*` DELIVER record → `delivery`) and composes `project_evidence` bound to the delivery `tree_sha` (deterministic + idempotent — no new files on re-projection); the `/lifecycle gate` engine command reads `<repo>/.devloop/ledger.jsonl` as plain data (hash chain **re-verified engine-side**, boundary-clean — no `scripts/devprocess` import), projects + runs `lifecycle_completeness`, and reports `READY`/`BLOCKED` **fail-closed** (default `--stages delivery` — the conservative default; `tests`/`reviews` are now logged by the driver (#830 wired the `log` seam to `ledger.append` in run.py / `build_real_ops`) and enforced via `--stages tests,reviews,delivery`). Covers the transition→stage mapper (every real shape + None cases, incl. a dry-run/**inert** review excluded from `reviews` #830), projection with fakes AND the real primitives, idempotency, fail-closed (empty tree_sha / missing stage / no slug / tampered / missing ledger), the command end-to-end, the engine↔`scripts/devprocess/ledger` hash cross-check, and the producer contract (driver `log` → a chain-intact ledger carrying the GATE/REVIEW transitions) | 43 |
| **Base-untouched reconciler** — the AD-8 delivered-state check (#601 S17): a full project lifecycle (mint/switch/stage/delete) under a throwaway workdir leaves the engine’s own source surface (`core/skills` + `engine/prompts`) **byte-identical** (a content-hash snapshot before/after, bytecode caches excluded), catching any path that resolves into the engine tree instead of the project root | 2 |
| **Read-only Memory MCP** — a dependency-free stdio JSON-RPC server exposing project memory as read-only search + deep-query tools, with a sealed-gated launch | 11 |
| **Open plugin surface** — discover and expose `skills/*` plugins with no core patch | 9 |
| **Extension SDK** — the curated public `ack.sdk` surface (contract, re-export identity, gate / schema / assemble) | 7 |
| **Packaged-plugin loading** — the `ironclad.plugins` entry point (root resolution, additive load, fail-soft) | 10 |
| **Export-leak guard** — internal artifacts kept out of the boundary and the public export | 4 |
| **Example plugin** — the shipped separate-repo example: discovers and runs via the loader and passes its own gate | 6 |
| **Playbook skill kind** — `SKILL.md` parse / validate / discover, progressive disclosure | 15 |
| **Skill generator** — spec → scaffold both skill kinds, schema-valid by construction (incl. free-text with quotes/backslashes/newlines) | 9 |
| **Paved-road generator** — `ack.generator` template-tree render + re-runnable 3-way merge + the built-in collision guard (a generated per-project item may not shadow a core built-in), plus the engine `/generate` seam that renders into the **active project's library** (ctx-resolved `vault/library`, never `core/skills`) with the built-in set injected as the guard (output_root + reserved set engine-enforced; bad-input handled as a clean error), plus the `--kind {case,prompt}` selector (default `case` byte-identical; `prompt` renders the `new-prompt` tree into a gate-valid `kind: prompt` item with a `locales/<lang>.json` overlay, and the prompt path widens the guard to also cover built-in prompt capabilities) | 30 |
| **Skill library catalogue** — manifest index, semver, provenance, install / update | 6 |
| **Skill registration gate** — doctor-preflight / schema-check / eval-gate, no unchecked code; the **generation-completeness gate** (`gate_generated`) additionally rejects an unfilled scaffold (the `ACK-SCAFFOLD-SENTINEL` marker) + **hermetic sibling-test execution** (`run_sibling_test_hermetic`: scrubbed env / hard timeout / isolated tmp; opt-in `gate_generated(execute=True)`); a **strict-locale variant** of the prompt gate (`gate_prompt(strict_locales=True)` — a declared non-source language whose `locales/<lang>.json` overlay is absent is a failure: "declared == delivered"; lenient fallback stays the default); and the **library-completeness invariant** (`library_items_complete` — gate every generated tool **and** every generated `kind: prompt` item [strict locales] in a per-project library; for S17 acceptance / operator); plus opt-in per-task `--validate-tasks` over the real base task records (DOCTOR-VAL) | 44 |
| **Skill lifecycle end-to-end** — generate → gate → register → load → invoke | 3 |
| **Shared content i18n** — the `ack.i18n` overlay loader (parameterized dir, fallback) | 7 |
| **Core built-in loader** — always-on built-ins from a fixed dir; plugins additive | 4 |
| **MPR core built-in** — router / registry / synthesis / audit / panels / templates / eval / packaging | 388 |
| **Prompt-library item** — `kind: prompt` parse / validate / discover + variable build | 7 |
| **Multilingual prompt assembly** — template + values → target language | 6 |
| **Prompt slash surface & elicitation** — list → guided ask-next → assemble + language | 9 |
| **Curated prompt library** — the shipped starter prompts: discover + gate + assemble (English + German) | 33 |
| **Discovery commands** — `/prompts` and `/skills` list the one loaded registry | 6 |
| **Per-item prompt invocation** — `/<prompt-name>` resolve + parse + elicit / assemble | 14 |
| **Orchestration state** — task lifecycle / dedup, initiative, autoplan, state end-to-end | 46 |
| **Project registry & isolation** — the installation-global project registry: atomic SSOT round-trip, a cross-process file lock (stale reclaim), >=64-bit mem_ns minting, the ADR-0007 reconciler (re-mint duplicates / quarantine missing roots / rebuild-from-disk), the implicit default project, and per-slug legacy migration, plus the request-scoped ProjectContext (contextvar resolver + copy_context thread-binding) routing the path roots, plus memory & warm scoping by the active mem_ns (ctx-aware _ids + scoped warm session + namespaced retrieval cache + ctx-bound generation workers + scoped MCP launch, plus the AST CI-lint forbidding raw scope-bearing globals outside the accessors; qualified-scope + cross-module from-import/getattr; scope-correct decorators/defaults; fully-qualified gx10 path + import-as alias + star-import + from-pkg-import-module), plus the project-overlay discipline (locked-key denylist + closed-allowlist + path-containment validator; parent-replacement bypass guards), plus the quiesced project-switch core (refuse-in-flight, save-leaving/load-entering session, ctx rebind, locked-config rebuild, save-leaving-then-load-entering ordering, rollback-on-failure, ctx_for-injection), plus the registry<->engine wiring (default binds the base partition behaviour-preserving, a registered project isolates, switch-back restores base; default re-points to the boot workdir; corrupt active-pointer repaired; init-failure clears ctx; default binds the boot workdir not a shared stored root; bind uses the per-process active cache; /switch + /project verbs end-to-end — no conversation bleed, leaving saved under its own root, in-flight refusal, cache updated after commit; corrupt-target no-bleed; rolling-summary dropped; last_response cleared; failed-save aborts; set_active-failure rollback), plus the **exec-cwd seam** (the model's file tools + `execute_command` + the launched code-agent resolve against the active project's root — `_exec_cwd`/`_resolve_exec_path`; absolute paths verbatim; default == the boot workdir byte-identical; no-context unchanged; empty-source move refused), plus the **code-agent memory scoping** (the launched code-agent's read-only Memory MCP env namespace + the worker / MPR reducer mirror write resolve to the active project's `mem_ns`; default partition byte-identical), plus the **project-scoped loader** (S11: the active project's `library/` is discovered alongside built-ins — last/additive + cross-root capability-guarded, byte-identical when absent; reloaded on `/switch` via build-then-swap; an unfilled scaffold is dropped at load — S11b-3a), plus the **per-track vault subtree** (S12a: a non-`main` active track is isolated under a hidden `.tracks/<track>/` subtree of the project vault — `_active_track` + track-aware `vault_root`; the default `main` track and no-context are byte-identical to the pre-track layout; an unsafe track id falls back to `main`), plus the **per-project + per-track vault-mutation lock** (S12b / Codex S3: a `vault_lock(pid, track)` distinct from the dev-loop `project_lock`, born-wired around the vault writers `initiative_new`/`reconcile_vault` — reentrant within a call stack so nested writers never self-deadlock, OS-serialized cross-process/thread, fail-soft when locking is unavailable; different tracks don't contend; the dev-pipeline macros `_stage_handover`/`_advance_pipeline` also run under it), plus the **typed-edge vault graph** (S12c: typed frontmatter edges [`depends_on`/`refines`/`supersedes`/`relates_to`/`implements`/`blocks`] → a deterministic, LLM-free machine SSOT `GRAPH.json` with full-relpath node keys [no timestamp ⇒ idempotent] + a human `LIFECYCLE.md` view, both generated next to `INDEX.md`; an unresolvable edge target is flagged `dangling`, not dropped; the generated files are excluded from the doc scan and the HTML markers stay frozen; aliased edge targets that resolve to the same doc collapse to one edge), plus the **composable lifecycle stages** (S12d: an ordered stage model `idea→design→adr→spec→tests→proposals→reviews→delivery`; a doc declares its `stage` in frontmatter and the initiative's lifecycle is COMPOSED via `lifecycle_state` [present / current / gaps / complete / counts / unknown], surfaced in `GRAPH.json` + `LIFECYCLE.md`; `can_advance_stage` is the fail-closed transition guard — forward-only unless `allow_regress`, unknown stages refused — the primitive the `/lifecycle` command [S16] + the DELIVER completeness gate [AD-7/S17] build on), plus **project-scoped + cross-track reconcile** (S12e: `reconcile_active_project` reconciles every initiative in the current track, and `reconcile_all_tracks` is the scheduled cross-track reconciler — it sweeps every track of the project [`main` + `.tracks/*`], **fail-closed per track**, idempotent; wired as `/initiative reconcile all`), plus **i18n of the vault/initiative chrome** (S12e-2: the German prose written into the generated vault docs [INDEX/LIFECYCLE] + the reconcile/initiative/mpr messages now route through `engine/messages.py` — English source/default [public-grade export], German overlay, language = `gx10.LANGUAGE`; the `ironclad:*:auto` HTML markers stay frozen and are never localized), plus the **evidence projector + lifecycle-completeness gate** (S13a / AD-7: `project_evidence(stage, title, body, *, tree_sha, content_hash)` appends a `type: evidence` stage-tagged doc bound to `tree_sha`+`content_hash` under `<slug>/evidence/`, deterministic filename + idempotent + append-only [never rewrites curated bodies]; `lifecycle_completeness(slug, *, required_stages, tree_sha)` is the fully-fail-closed DELIVER-leg gate — every required stage present AND bound to the delivery tree_sha; tree_sha/content_hash hex-validated [no filename traversal]; CRLF-normalized for idempotency), plus the **per-track memory sub-scope** (S14-1: `ProjectContext.mem_scope()` composes `<mem_ns>::track::<tid>` for a non-`main` track — flowing through both the cold partition `MemoryManager._ids()` and the warm session/cache namespace; `main`/no-ctx/empty-ns byte-identical; an unsafe track falls back to `main`, matching the vault subtree; the vault + memory track-safety predicates agree for every input incl. non-str), plus the **cross-switch memory re-keying lock** (S14-2: a single build-once `MemoryManager`/warm handle follows the active scope **per call** — `/switch` re-keys via the context, no rebuild — proven across A→B→A switches + the track dimension + registered-project-never-base; the installation-global, overlay-locked connection makes a "rebuild-on-switch factory" unnecessary) | 247 |
| **Dev-process public facade** — the curated, versioned `ack.devprocess.api` seam: import + version, graceful degrade (every verb raises `SubstrateUnavailable` with no driver), the `runtime_checkable` driver protocol, set/get-driver registration, the five verbs delegating to a registered driver, the optional-task_id create-new shape, the required deliver ledger_path, and the curated `__all__` surface, plus the in-engine driver wiring (registered at gx10 import; `stage_handover`/`advance` delegate to the real impls late-bound; `select_unit`/`deliver`/`record_feedback` report unavailable; the stage_handover/advance_pipeline tool dispatch routed through the facade, with a direct-impl fallback when no driver; the real-launch import-order (core/engine-only on sys.path) resolves the late facade import; registration never clobbers a richer pre-registered driver) | 16 |
| **Parallelism** — governed fan-out, the in-engine tool, single-writer reduce, the parallel router | 29 |
| **Thin client + BYO code-agent** — the agent pool, a configurable agent-command template, managed transport, the config-driven code-agent registry, a per-agent boot probe, result classification, and onboarded-but-disabled agents | 85 |
| **Runtime-aware output & language** — encoding safety, color gating, reply language | 14 |
| **Token budget / context trimming** — token-accurate budgeting, a pre-flight overflow guard with emergency trim, the char-fallback watermark reserving sys+tools+thinking, and live context-length discovery | 59 |
| **Misc** — manual cat tool, orchestrator version | 7 |
| **Demo vessel** — the example-workspace doctor preflight | 1 |
| **Documentation & release integrity (internal QA)** — documentation-reality checks, the generated roadmap and test counts, export-sync verification, the clean-room pre-publish proof, deploy-consistency checks, and the maintainers' release-process guards | 72 |
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
