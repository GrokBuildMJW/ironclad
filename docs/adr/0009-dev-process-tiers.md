# ADR-0009 — Dev-process tiers (DEV 1, DEV 2, tier 3) as capability presets

**Status:** accepted. Builds on ADR-0004 (extension SDK) and the dev-process guard. **Amended by [ADR-0011](0011-dev-process-rework-project-isolation.md):** DEV-2 and tier 3 merge to a single native tier, and locality (in-engine vs github) becomes an orthogonal `exec_mode` axis rather than a tier. **The resolver/presets described below are now PRIVATE** (a private tree, not the wheel); the public dev-process surface is the three DEV-1 prompts only — the `ack.devprocess` / "in the wheel" wording below is historical.

## Context

This work ports the end-to-end development process into Ironclad as **three switchable tiers**:

- **DEV 1** — light: prompts + skills only, no native guards.
- **DEV 2** — native guards run in Ironclad; GitHub push is provided but switchable.
- **tier 3** — the fullest hardness tier, a plugin-provided extension capability;
  a deeper orchestration provided by the extension.

A tier must be *selectable* and the selection must actually gate which capabilities run.

## Decision

A tier resolves to a small bundle of **capability flags** (`ack.devprocess.tiers`, public):

| flag | values | meaning |
|---|---|---|
| `guards` | `off` \| `native` | the native composed GATE + coupling guards |
| `push` | `off` \| `switchable` \| `on` | the GitHub-adapter seam (`switchable` = provided-but-opt-in) |
| `orchestrator` | `prose` \| `native` \| `engine` | DEV1 discipline · DEV2 local driver · tier 3 a deeper orchestration provided by the extension |
| `extension` | `off` \| `on` | a plugin-provided extension capability (tier-3-only) |

**Presets** (cumulative): DEV1 = all off/prose; DEV2 = guards `native`, push `switchable`,
orchestrator `native`; tier 3 = push `on`, orchestrator `engine`, extension `on`.

`resolve(tier, plugin_active)` is the SSOT for what each tier *means*. **Historical (superseded by
[ADR-0011](0011-dev-process-rework-project-isolation.md)):** this ADR originally had the operator
select `config.dev_process.tier = 1|2|3` and the engine apply the resolved flags. The engine **does NOT read
`config.dev_process.tier`** — the shipped tool runs DEV-1; the resolver/presets are a PRIVATE dev-loop detail
(a private tree), and a project is bound to the INTERNAL (extension-driven) process per-project via the
injection descriptor (a per-project runtime side-file, mutually exclusive with the normal process), not
a global tier switch. **tier-3 flags that need the extension plugin** (`orchestrator=engine`,
`extension=on`, `push=on`) are **inert without an activated plugin** → tier 3 degrades to the DEV-2
value for those, so the extension orchestration is never advertised when it isn't installed.
An unknown tier is **fail-closed** (never a silent default).

## Public / extension boundary

The resolver + presets are **public** (`ack.devprocess`, in the wheel — ADR-0004 packaging rule). The
*behaviour* the flags gate is built per tier: DEV1/DEV2 capabilities are public in `core/`; the tier-3
extension orchestration lives in **an extension plugin** and is only reachable when that
plugin is present + activated (the `plugin_active` input). No private identity enters the public
resolver or its docs.

**DEV-1 is stateless.** DEV-1 has **no work-item store / backlog** — it is a
discipline-only, single-unit runner (the operator picks one unit; the loop is the prose
`dev-loop-runner` prompt, finished before the next). `is_stateless(tier)` makes it checkable: True iff
`orchestrator == 'prose'` (DEV-1); DEV-2/tier 3 run the native driver (transition ledger + artifact-derived
resume) and are not stateless in that sense. The work-item state for DEV-2/tier 3 lives in the engine, not
a DEV-1 store.

**Runtime-guard binding.** `runtime_guards(tier)` ties the engine runtime guards
(`boot-probe`, `task-class-routing`, `budget-breaker`, `failover`, `distinct-reviewer`,
`web-search-trust-gate` — each built + tested in `core/engine`) to the tier capability: a tier whose
resolved `orchestrator` is not `prose` (DEV-2/tier 3, the native engine) gates with the full set, DEV-1
(discipline-only) with none. The explicit, checkable contract that DEV-2/tier 3 inherit the runtime
guards; the engine reads it alongside the flags.

## Consequences

- Later units register their behaviour behind a flag rather than hard-wiring a tier check; the flag
  vocabulary is the contract.
- The three tiers are presets over composable flags — the default UX is the named
  tier, but the flags remain individually inspectable/overridable.
- Pure + offline-tested (`test_tiers.py`); GitHub-agnostic.
