# Project isolation

Every Ironclad engine session runs inside a **project**: a named, isolated unit with its own state and
vault paths and its own memory partition. A single-project install needs to do nothing — the implicit
`default` project keeps the pre-isolation behaviour byte-for-byte. Multiple projects let one installation
keep separate work fully partitioned (separate `vault/`, separate `.ironclad/` machinery, separate
memory) and switch between them within a running engine.

## Model

- **Registry (installation-global SSOT).** A single `registry.json` under `GX10_HOME`
  (`%LOCALAPPDATA%/ironclad` on Windows, `~/.ironclad` elsewhere; override with `GX10_HOME`) lists every
  registered project and the **persisted active pointer** (continuity across reboots). Writes are atomic
  (temp + fsync + replace) under an **OS file lock** released by the kernel on process death, so there is
  no stale lock to reclaim. A corrupt registry is atomically quarantined as a non-clobbering
  `registry.json.corrupt.*` sidecar before boot creates a fresh registry, preserving project roots and
  memory keys for operator recovery. The index self-heals: a malformed entry is skipped, a
  duplicate/low-entropy memory key is re-minted, and projects can be reconstructed from on-disk roots.
- **Project.** `id` · `slug` · `root` (absolute) · `mem_ns` (a ≥64-bit memory-partition key, registry-
  verified unique) · `tracks`/`active_track` · `created`.
- **Active is single-active per process.** The engine reads the persisted active pointer **once at boot**
  (continuity) and caches the active project for the process. Background threads (the reconciler, request
  handlers) bind that cache — they never re-read the registry per request — so a second engine sharing the
  same home can never re-point a running one, and there is no per-request file I/O.
- **The `default` project** binds **this process's working directory** as its root and the **legacy/base
  memory partition** (an empty `mem_ns`, so memory and the warm cache fall back exactly as before). This
  is why an existing single-project install is unchanged.

## Commands

```
project list                 list registered projects (* marks the active one)
project new <name> [--path <dir>]
                             mint a fresh isolated project (root = --path or <cwd>/<slug>, minted mem_ns,
                             made active; seeds the first software vault unit) — the guided setup command
project use <slug>           select an existing vault unit inside the active project
project active               show the active project
project track new <t>        create a parallel track AND switch to it (like git checkout -b)
project track use <t>        switch to an existing track (vault + memory follow)
project track list           list the active project's tracks (* marks the active one)
project delete <id> [--purge]  forget the project's memory + drop it from the registry (dirs kept unless --purge)
project archive <id>         hide a project (reversible; data + memory kept; not switchable until unarchive)
project unarchive <id>       restore an archived project
project list --all           include archived projects in the listing
switch <project_id>          rebind this engine to a project
generate [--kind case|prompt] --domain <d> --case <c> --description <text> [--prefix x] [--dry-run]
                             render the paved-road template tree into the ACTIVE project's library
                             (--kind case = a CASE+run tool [default]; --kind prompt = a kind: prompt item)
```

## Per-project library

`generate` renders into the **active project's library** — a `library/` subtree under the ctx-resolved
`vault_root()` (so each project's generated capabilities live under its own root, never in `core/skills`).
The engine injects the set of **core built-in capabilities** as the generator's collision guard, so a
generated item can never shadow a built-in (it is refused fail-closed before anything is written). Under
the implicit `default` project this is the boot workdir's `vault/library` — the same place a single-project
install would write.

The **loader discovers** the active project's library alongside the built-ins: it is the **last, additive**
skill/prompt root (after the core built-ins, the global plugins dir, and packaged entry points), so a
generated capability is offered next to the built-ins — and, being last under first-kept discovery, can
never displace a built-in. A project with no library is byte-identical to a single-project install. An
**unfilled scaffold** (a generated tool still carrying the `ACK-SCAFFOLD-SENTINEL` marker) is **dropped at
load** — it is never offered as a real tool until it is implemented (the cheap generation-completeness gate
enforced on the load path; the full hermetic sibling-test run is the `library_items_complete` invariant —
operator / self-dogfood acceptance, not auto-scheduled).

## Tracks

A project can carry parallel **tracks** — first-class, isolated lines of work within the one project, with
**single-active execution** (exactly one track is active at a time). The active track is part of the
request-scoped context; the default track is **`main`**. Each non-`main` track gets its **own vault
subtree** under a hidden `.tracks/<track>/` directory of the project vault, so the initiatives, INDEX, and
generated artifacts of one track never mix with another's. The `main` track resolves byte-identically to the
pre-track layout, so a single-track project is unchanged. Track ids that could escape the subtree (path
traversal or separators) fall back to `main`. Vault mutation is **serialized** per project+track by a
`vault_lock(pid, track)` (distinct from the dev-loop project lock) wrapped around the vault writers —
reentrant within a call stack, OS-serialized across processes, fail-soft. A non-`main` track also gets its
own **memory sub-scope** `<mem_ns>::track::<tid>` (`ProjectContext.mem_scope()`), flowing through both the
cold partition and the warm session/cache, so a track's memories + rolling summary are isolated too; the
`main` track is byte-identical to the bare `mem_ns`. (The per-track ledger is layered on in a later step.)

Tracks are managed from the CLI with `project track new|use|list`: `new <t>` creates a track **and** switches
to it (create-and-switch), `use <t>` switches to an existing one, and `list` shows them. A switch rebinds the
engine context and reloads the per-track library, so the new track's vault subtree and memory sub-scope take
effect on the next turn.

## What a switch does

`switch <project_id>` performs a **quiesced** rebind, in this order:

1. **Refuse** if a dev unit is currently in-flight for the leaving *or* the entering project (a held
   project lock) — busy work is never switched out from under or into.
2. **Save** the leaving project's conversation under **its own** root.
3. **Bind** the entering project's context: `state_root()` / `vault_root()` now resolve under the target
   root, and memory + the warm rolling summary scope to the target's `mem_ns`. The model-driven file
   tools (read / write / list / search / …), `execute_command`, and a launched code-agent also run with
   the target root as their working directory — the switch does **not** `chdir` the process (a global
   `chdir` under the daemons / fan-out threads would be unsafe), so the exec cwd is threaded through the
   active context. Absolute tool paths are honoured verbatim; the `default` project resolves to the boot
   workdir, byte-identical to before.
4. **Rebuild** the effective config for the project from the deployment base (a project overlay may adjust
   non-locked keys; **locked** keys — `connection`, `security`, `setup`, `search`, `generation`,
   `plugins_dir`, `providers.budget` — can never be re-pointed by a project).
5. **Swap** the conversation: load the entering project's saved session, or start fresh — the live
   conversation is **replaced**, never appended, so nothing bleeds between projects. The system prompt is
   project-independent (the engine's operating instructions) and is preserved. After every switch attempt,
   rebuild the live reply-language directive so it agrees with the final effective config — whether the attempt
   succeeds, re-asserts the same project, is refused, or rolls back.
6. **Reload** the skill/prompt registries (after the active pointer is committed) so the entering project's
   library is discovered and the leaving project's is dropped — **build-then-swap**: discovery runs into
   fresh dicts, then the live registries are swapped in, so a failed or slow reload never empties them. The
   reload runs after the commit, so it can never fail an already-committed switch.

If any step fails the context is rolled back to the leaving project and the active pointer is left
unchanged (it is committed last), so a failed switch never leaves a half-switched engine.

## Memory partitioning

A project's `mem_ns` is the cold-memory partition (`agent_id`) **and** the warm-cache namespace, so two
projects never see each other's memories. The `default` project uses the base partition (empty `mem_ns`),
which is the legacy un-namespaced store — existing memory stays readable after the feature is activated.

The partition reaches the spawned **code agents** too: the launched read-only Memory MCP carries the
active project's `mem_ns` in its env (derived from `_active_mem_ns`), and — on a separate path — the
single-writer worker / MPR reducer mirror write resolves to that same `mem_ns` through the
`MemoryManager._ids()` chokepoint. So a code agent reads and writes only its project's memory. With no
active (or the `default`) project both fall back to the base partition, byte-identical to before.

Each stored cold memory also **self-describes its origin scope** (the active `mem_scope`) in its metadata,
so a memory's provenance survives independently of its `agent_id` partition (used for promotion eligibility
and cross-partition audit). The tag is added **only when a project scope is bound**, so the base partition
is byte-identical.

**Forget (scope-targeted delete).** Forgetting a scope removes one partition across all tiers. A non-empty
`mem_scope` is required — the shared base partition can never be wiped by a scope-aware forget — and each
tier **fails soft**, so a down store never breaks the call. Cold memories are deleted by `agent_id` through
the cold store's fail-closed `/delete_all` route (an all-empty filter is refused, never a full wipe); the
warm tier drops the **exact**-scope session and retrieval-cache keys without sweeping deeper track scopes
(`<mem_ns>::track::<tid>`) or prefix-matching siblings; the lesson tier delegates to its provider.

## Lessons

The lesson tier layers on the same memory substrate through the stable `ack.lessons` seam: project-private
notes that carry "what worked / what to avoid" across turns. Ironclad owns the **substrate + a stable
delegation API**; the lesson **semantics** (distillation, ranking, the persistent backend) come from a
registered **provider** — with no provider wired the whole tier is a fail-soft no-op, byte-identical to an
engine without it. See [the LessonStore API](lesson-api.md) for the public verbs.

- **Project-private by default, promote-gated otherwise.** A lesson is stored under the active `mem_scope`,
  so lessons never cross projects. Broadening to a wider scope (for example a curated global store) happens
  **only** through a redaction-gated `promote()` — an explicit redactor must approve the redacted text; a
  missing or refusing redactor causes refusal (fail-closed), so a private lesson is never promoted unredacted.
- **Engine wiring is advisory and fail-soft.** When staging a task + handover the engine appends a scoped
  lesson **brief** alongside the Memory brief; when completing a task it **reports** the feedback as a scoped
  lesson. Both are byte-identical no-ops until a provider is registered.
- **Participates in scope-aware forget.** `forget(scope)` is delegated to the registered lesson provider
  when it implements the optional verb; otherwise it is a no-op.

> The registry is generic project-isolation infrastructure. Any per-project *role* descriptor a deployment
> layers on top (for example a development-process target) is a separate, private overlay keyed by project
> id and is kept out of this public registry.
