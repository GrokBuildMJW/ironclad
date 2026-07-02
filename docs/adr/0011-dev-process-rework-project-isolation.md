# ADR-0011 — Dev-process rework: internal DEV-2/3, public DEV-1, and the project-isolation spine

- **Status:** Accepted (planning ratified; implementation in progress). **Supersedes [ADR-0010](0010-dev-process-substrate-split.md)** (the DEV-2 public-substrate split) and **amends [ADR-0009](0009-dev-process-tiers.md)** (the tier model). Epic #601 (reworks #532).
- **Date:** 2026-06-27
- **Context sources:** an operator review of the DEV-2-public direction; a design pass + an independent adversarial review (high effort) of the project-isolation and memory model; and the runtime-context fact that the engine + reconciler run on bare runners with no `ack` on `sys.path`.

## Context

ADR-0010 publicized the dev-process **substrate** (`ack.devprocess`) so the framework — not the orchestrating CLI — runs the guards. In review this proved to be the wrong boundary: the substrate's only real consumers were always private (the reconciler + the dev-loop runner), the public surface bought nothing, and it cost ongoing clean-room/export plumbing. Separately, three capabilities were missing for the dev process to run as a **product**: per-project **isolation** (a project's filesystem, version-control usage, and system prompts must have **zero effect on the delivered engine state**), a faithful **in-engine** rehearsal of a private+public repo pair (no real remote forge), and a **two-layer memory** (global + per-project). The engine is structurally **single-tenant** — its roots, config, and memory handles are frozen once at boot — so all three needs collapse onto one missing abstraction.

## Decisions

**D1 — Reverse the publicness, keep the engineering.** The dev-process substrate returns to **monorepo-private** (`scripts/devprocess/`), reached by the private dev-loop + reconciler through the existing file-load seam; the wheel returns to `packages = ["ack", "ack.lodestar"]` *(refined by AD-3 / D2 below: the one curated exception — the versioned `ack.devprocess.api` facade — was kept, so the shipped list is `["ack", "ack.lodestar", "ack.devprocess"]`, facade-only; the implementation substrate stays out of the wheel)*; the code-agent runner is private. The already-built modules are **relocated, not rewritten** (the move preserves the engineering; only the *public framing* is undone — none of it had shipped). DEV-2/3 are an **internal extension** of the framework, activated through a thin, secret-gated driver seam; the code stays monorepo-private.

**D2 — Public DEV-1 is data, not API.** The public surface of the dev process is exactly the **three prose prompts** (`dev-process`, `verbatim-scope-audit`, `dev-loop-runner`, EN/DE) shipped as `kind: prompt` built-ins. No public Python dev-process API. The one deliberate future public seam is a curated, [ADR-0004](0004-extension-sdk.md)-versioned `ack.devprocess.api` facade — the sole stable delegation target for generated per-project tools — because private engine internals are unimportable in the clean-room; the isolation substrate itself stays engine-internal (runnable scripts, not the wheel).

**D3 — DEV-2 and DEV-3 merge; locality is an orthogonal axis.** The tier model of ADR-0009 collapses to **{1 = DEV-1 prose (public), 2 = native (private)}**. What was "DEV-3" is no longer a third tier but an **orthogonal execution-mode axis** `exec_mode ∈ {in-engine, github}` selecting a **vendor-neutral Forge backend**: `github` drives real issues/PRs/merges; `in-engine` drives **two real bare git repos + a JSON object store** (a faithful private+public repo-pair simulation, located outside the dev project's git tree) with no remote forge. The pure adjudicators and the frozen MERGE/DELIVER gate are **mode-invariant**. `exec_mode` is **immutable per project**, with completion idempotency keyed on `(issue, release_index, mode)` and a reserved `local-*` release-index namespace disjoint from real publish targets, so an in-engine rehearsal authorization can never replay into a real publish. The legacy `type` of a work unit (e.g. reasoning-only vs software) survives as a per-project **`workflow_mode`** with the same hard gates — it is behavioural, not cosmetic.

**D4 — The project-isolation spine: a registry + a request-scoped context.** A persistent, installation-global **Project Registry** (server-resolved home, reinstall-safe, atomic writes + a cross-process lock, reconstructable from on-disk project dirs) is the source of truth for registered projects. It feeds a request-scoped **`ProjectContext`** that **replaces** the boot-frozen module globals: all path, config, catalogue, and memory resolution route through the active context. A **single-active-project** model with a **quiesced transactional switch** (drain-or-refuse in-flight work, flush the in-process conversation, re-key + reload persisted per-project state, rebuild config/memory/catalogue/prompt, rebind background daemons) delivers isolation without process-per-project complexity. **A dissolve, not a retrofit:** the legacy single-active "initiative" mechanism is replaced — the **project** becomes the one registered, isolated unit (it absorbs the proven vault/lifecycle/reconcile machinery directly); within a project, parallel **tracks** are first-class isolated entities with **single-active execution**. The control-plane facts that made the engine single-tenant (a global active pointer, global memory keying, build-once memory handles) are removed; background daemons read the active project from the registry each tick and spawned workers bind the context explicitly. Guided setup is the existing `/`-command, elevated.

**D5 — Two-layer, scope-partitioned memory with a stable lesson seam.** Memory is partitioned by `agent_id = mem_ns` (never `run_id`) across **base-runtime / curated-global / project-private / project-public** scopes (and a per-track sub-scope); the **curated-global (delivered) layer gets a physical store boundary**, projects share a collection partitioned by `mem_ns`, and promotion between layers is **explicit + operator-gated**. Actionable lessons (a separate loop-intelligence concern) are a **scope-partitioned tier on this substrate, never the global session**: this rework owns the substrate (scope-keyed sessions + caches, scope-aware non-lossy reflection, a stable **`LessonStore`** delegation seam, scope-aware delete/forget) and the lesson *semantics* are supplied through that seam only. Lessons default **project-private**; promotion to public/curated requires a redaction gate; cross-project preferences are a two-layer (global-default + project-override) read.

**D6 — Isolation guarantees are structural, and only as strong as their weakest enforcement.** "No effect on the delivered state" is enforced by six fail-closed structural guards (path containment, no base-memory writes, generator never targets `core/skills`, vault export-exclusion, credential non-reach, in-engine deliver allowlist), each with a negative test, all exercised by **one canonical self-dogfood acceptance test** (Ironclad develops Ironclad in a separate checkout, asserting the installed engine + config + built-ins are byte-unchanged across a full create→switch→run→deliver→switch-back cycle). **Hard delivery isolation exists only for `in-engine` mode** (local bare remotes, no token); per-project **`github`-mode delivery for a non-self project stays operator-gated** until just-in-time least-privilege credentials / process isolation land — the env-layer credential scrub is best-effort and that limit is documented, not overclaimed.

## Consequences

- ADR-0010's public `ack.devprocess` modules + tests leave the wheel/clean-room; the substrate + its tests live in a private tree run by the private CI (excluded from export, clean-room, and the public test counts). The dev-loop runner is private.
- ADR-0009's three-tier preset model is re-expressed as two tiers + an orthogonal `exec_mode`; the tier presets and the plugin-gated degrade are reworked accordingly (a semantic re-expression layered on the existing, complete driver/e2e/coupling modules — not a re-privatization of finished work).
- The single-tenant control plane is rebuilt (registry + `ProjectContext`); this touches every path/memory resolution site and the background daemons/workers — the highest-risk change, gated by a feasibility spike before the refactor.
- ADR-0009 and ADR-0010 files are **kept** (history); the inbound statements that asserted the now-reversed public substrate are marked historical where they would mislead.
- The decomposition executes against this ADR: the relocation as the foundation, then the registry → context → switch → forge → memory → guided-setup → acceptance series.

## Amendment — internal dev-process injection (epic #974)

The `exec_mode` axis this ADR introduced is realised as a **per-project injection descriptor** (epic #974),
so an operator can run the tool on a project **with** the internal (GitHub-integrated, extension-driven) dev
process, while the **normal** (public DEV-1) process is refused on that project — mutually exclusive, stable.

- **Storage & lifecycle.** The descriptor is a runtime side-file `<devloop_home>/dev-target.json` (per-project,
  co-located with the ledger), **separate** from the delivery target table (`spec.TARGETS` is untouched) and
  **not in the wheel**. Schema `{project_id, exec_mode∈{github,local}, tier∈{2,3}, plugin_required, plugin_id?,
  health_gate?}` (`devprocess.spec.validate_injection`). It is bound atomically under the project's registry
  lock (`devtarget.bind` / `devtarget.py register`), removed with `unbind`, and reconciled fail-closed
  (`devtarget.reconcile` + the engine's `_dev_target_drift` at the `/lifecycle` gate). The engine reads it as
  **plain data** (`gx10._dev_target_descriptor`) — it never imports the private dev-loop machinery.
- **Validation / health / fail-closed gates.** (1) The internal driver refuses to start when the descriptor
  requires the extension but the plugin is not present + healthy (`devtarget.plugin_active` + a rewired
  `entry_plan` that returns a `blocked`/`refused` outcome instead of silently degrading tier-3 → native).
  (2) The normal in-engine pipeline (`stage_handover` / `advance` / `TaskStore`) is refused on an internal
  target (`gx10._internal_target_blocks_normal`). (3) `/switch` serializes under a repo-global lock.
  (4) Every ledger record is stamped with `project_id` + `exec_mode`; a reader detects a fork and refuses to
  mis-resume (`ledger.fork_reasons`). Each fail-closed path has a negative test, plus a self-dogfood
  isolation test (both sides refuse on one project).
- **tier-2 → tier-3 migration (no ledger fork).** Re-binding a project from tier-2 to tier-3 keeps the same
  `project_id`, so the ledger's `project_id` stamp is unchanged and `fork_reasons` sees no project fork; the
  `exec_mode` stamp is what a reader compares, so a mode change is caught **within a unit** (refuse) while a
  clean re-bind between units simply starts stamping the new mode. `is_base_project`'s fail-safe-to-base
  (never relocate on a registry read error) is unchanged — the ledger fork detection is the safety net.
- **Operator view.** `/status` shows whether the active project is an internal target (its exec_mode / tier /
  plugin) or runs the normal process; the REFUSE messages name the reason (plugin inactive / internal target
  / ledger fork).
- **Public tool = DEV-1.** No `dev_process` runtime switch ships; the internal process + its extension +
  descriptor are private. This supersedes ADR-0009's operator-tier-selection wording.
