# Changelog

All notable changes to Ironclad are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/); the project is **pre-release** (0.0.x).
Released versions are listed below.

## [0.0.30]
### Security

- **Secure deployment defaults (#1469 F8):** fresh servers bind loopback under the unauthenticated `open`
  profile and refuse non-loopback exposure unless token/sealed auth or the named
  `security.allow_unauthenticated_bind` override is explicit. Search and forge now require explicit enablement;
  Claude coders no longer default to `bypassPermissions` or `--dangerously-skip-permissions`, with bypass
  restored only by a per-agent capability opt-in. Connection, first-token, and provider-CLI timeouts now have
  finite defaults and hard ceilings; worker/autopilot concurrency, retry, and autoplan task budgets are bounded
  and autonomous writers reject zero-as-unlimited. Existing private deployments retain their prior behavior via
  explicit private config rather than weaker public defaults.

### Added

- **Generated configuration reference and switch parity guard (#1470 F9):** `config-runtime.md` now derives
  every effective leaf, tombstone/alias, external memory/warm seam, and operational-switch inventory from
  the typed schema. Private CI rejects byte drift, requires schema/documented/AST-read switch equality,
  flags raw hidden boolean gates, and proves the complete schema boot-only set matches both runtime and
  command-spec frozen metadata, including against the staged public export.

### Fixed

- **Bounded server-side edit reads (#1498):** `edit_file` now shares the #1488 byte-bounded full-text
  reader on its read-modify-write path, refusing a multi-GB target before allocation while preserving complete
  under-cap content and newline normalization. The model-facing `review(paths=...)` reader and user-project
  vault document scanner use the same bound; `edit_file` remains server-side only with no Ink twin.

- **Release hygiene:** raised the build-system `setuptools` floor to `>=77` to match the PEP 639 SPDX
  `license` metadata the package uses, and corrected public documentation — the MPR README now reflects MPR as
  an always-on core built-in (default on; no separate deploy step) instead of the retired loadable-plugin flow.

- **Fail-closed MPR retention (M19, #1495):** the reserved §9 `prune_runs` utility now protects a run
  whose provenance is unreadable, missing, or incomplete (a provenance object without a `violations` key)
  exactly like a violation, because its violation status cannot be proven clear. A directory deletion must also be verified complete before its TaskStore index entry is
  dropped or the run is reported deleted; a failed deletion is no longer reported as success.

- **Serialized Ink session heartbeats (M26, #1492):** the Phase-d heartbeat is now a serial,
  self-scheduling loop that rechecks shutdown state after each network wait. Heartbeats and session reopens
  cannot overlap, a heartbeat that resolves after `stop()` cannot reopen the channel, and a reopen already
  in flight at shutdown is closed instead of leaving the sealed server session live until its TTL expires —
  `stop()` awaits the in-flight tick, so the server is sealed by the time it returns (robust even if the caller
  exits immediately afterwards). A surprise error in a heartbeat can no longer permanently kill the loop.

- **Shell-safe client updates (M28, #1496):** `/update` no longer interpolates the configured source path
  into shell command strings. Update steps are executable-plus-argv launches, run without a shell on POSIX
  and through an argv-only `cmd.exe` wrapper for Windows npm compatibility, while source paths containing
  shell/cmd metacharacters are refused before any process starts.

- **Transactional multi-file generator updates (M22, #1494):** the paved-road generator now renders and
  decides every target before committing all changed files and the merge-baseline state in one
  rollback-protected phase. A target or state write failure restores prior file/state bytes (and removes
  newly created targets), so a partial upgrade can no longer leave the on-disk tree ahead of stale state.

- **Atomic catalogue install and update (M21, #1493):** tools and playbooks are now copied to a
  same-filesystem sibling staging path before an atomic swap. The working version remains live until the
  replacement is complete, failures roll back and clean staging/backup paths, and playbook overwrite no
  longer deletes the installed directory before `copytree` succeeds. Backup removal after a successful swap is
  best-effort (a lock can no longer turn a completed install into a reported failure), a double fault where the
  swap and the restore both fail preserves the original in the backup instead of deleting it, and skill
  discovery skips hidden (`.`-prefixed) directories so a leftover staging/backup copy can never shadow the real
  skill with a stale version.

- **Prompt tool-result delivery after transient transport failures (#1490 M27):** Ink now drains its
  buffered `/tool-result` queue on every connected status poll instead of waiting for a reconnect edge,
  while the Python client retries network, timeout, and HTTP 5xx failures with bounded backoff. Both clients
  still drop permanent HTTP 4xx rejections, preventing a brief result-post blip from stalling `ToolBridge`.

- **Bounded code-agent execution (#1491):** Python and Ink handover clients now enforce the live
  `code_agents.timeout_s` wall-clock, launch each coder in a dedicated process group, and kill its complete
  process tree on timeout so a hung agent cannot retain a worker claim forever. Ink also retains only bounded
  stdout/stderr tails, preventing an unbounded coder stream from growing client memory or its diagnostic log.

- **Whole-tree model command termination (#1489):** Python and Ink now launch `execute_command` in a dedicated
  process group and kill and reap every descendant on timeout or cancellation. Bubblewrap also uses
  `--die-with-parent --unshare-pid`, preventing a sandboxed descendant from surviving its parent.

- **Bounded model-tool filesystem I/O (#1488):** Python and Ink `read_file`, `list_directory`, and
  `search_files` now enforce byte, entry, file-scan, and hit ceilings before allocating or decoding input.
  Oversized reads are refused, directory iteration stops after the cap-plus-one overflow probe, and search
  short-circuits with an explicit truncation marker instead of walking or loading an unbounded file/tree.

- **Atomic runtime configuration application (F6b, #1467):** `/config set` now deep-clones and validates
  the complete candidate, derives unpublished runtime state, commits globals and reversible integrations
  under a config lock, and publishes the candidate only after total success. Validation, derivation, commit,
  and hook/store/thread failures restore the prior runtime snapshot, retain the original config tree, and
  emit one red refusal without a green success; startup validates the final merged multi-profile projection
  before publishing config or runtime state.

- **Typed configuration control plane (F6a, #1467):** derive complete code defaults and boot-only metadata
  from one stdlib-only leaf schema; strictly reject ambiguous file/dict booleans, invalid enums/ranges and
  relationships, unsupported worker write modes, and incomplete multi-tenant enablement while using one
  explicit environment boolean parser.

- **Dead and contradictory runtime switches are retired or renamed (#1468 F7).** Provider enablement now
  derives only from `setup.type`; dead provider-scoring, legacy-lesson, product-conflict, and watcher config
  leaves are warning-only tombstones. `framing_notes.enabled` and `process.hints_enabled` replace their legacy
  names through one-release aliases, with the former scoped only to framing capture and the latter only to
  ACE-backed pre-turn hints. `/auto on|off` owns watcher state without a config mirror, and live quality tuning
  atomically rebuilds the always-on breaker while retaining bounded score history, preserving a live latch or
  recovered state, and recognizing only a newly sustained trailing low-score streak.

- **Finite failure strategy and progress heartbeat are always on (#1466 F5b).** The retired
  `strategy.enabled` switch is now a warning-only tombstone; every failed coder run is classified and charged
  to a positive bounded per-task attempt budget. Spending that budget durably marks the task
  `blocked_kind="escalated"`, emits the human-escalation hook, stops `/feedback` before breaker/no-feedback
  retry handling, and excludes the terminal task from pending clients, claim/unclaim reopening, and manual or
  reconciler launches. The heartbeat now defaults to a finite 900 seconds and refuses zero, negative, or
  non-finite tuning, while deliberately preserving the no-false-positive rule for manually managed tasks that
  have never produced a coder-log or feedback signal.

- **Mandatory validation and anti-guessing at staging (#1466 F5a).** ACK task validation, required
  deterministic verifier rules, ambiguity detection, and the output-quality breaker are now always on.
  Create and re-hand staging share one ordered pre-write boundary; validation/verifier/detector failures and
  latched quality trips refuse before `TaskStore.create()` or handover writes. Legacy enable keys are
  warning-only tombstones, while grounding remains advisory and quality/grounding thresholds remain tuning.
  Stored blocked-state annotations no longer invalidate recovery re-hands, and an at/above-threshold verifier
  score now resets a latched quality hold so a passing submission can resume staging. An available memory tier
  with no grounding hits is now recorded as unavailable instead of a false `0.0` grounding failure, while
  partial evidence remains a real degradation signal. Detector-internal ambiguity errors refuse staging, and
  the new `/quality reset` operator command guarantees recovery from any latched hold and threshold setting.

- Learned-state adaptation is always a snapshot + deep-candidate + atomic-promote-or-quarantine transaction
  (#1465 F4). An injected evaluator promotes only a measured non-regression and quarantines regressions or
  unavailable scores. Without one, the default structural gate promotes success- and failure-learning unless
  the candidate empties or loses more than 50% of a non-empty playbook; catastrophic loss is quarantined and
  the snapshot remains the rollback net. The retired `ace.safe_promote` key is a warning-only tombstone.
- Egress enforcement is always on and fail-closed (#1464 F3c). The retired
  `security.egress_analysis.enabled` / `GX10_EGRESS_ANALYSIS_ENABLED` controls are warning-only tombstones;
  posture is declared solely by `network: none|declared|open` in the approved design `## Build policy`.
  Designs with no build-policy section advance without analysis, while a present section with a
  missing/invalid posture and restrictive-posture no-root or analyzer failures refuse advance.
- Model command isolation and untrusted-result fencing are mandatory and fail closed (#1464). `security.sandbox`
  now accepts only `auto|bwrap|firejail` (default `auto`); legacy `off`/`none` values warn and are ignored.
  Missing/failed backends and Windows refuse before any subprocess, including the versioned client-bridge lane,
  while Ink `/sh` remains a separate explicit operator channel. Production Linux must provide `bwrap` or
  `firejail`. Every untrusted serialized result—including web search, parallel/provider reasoning, memory,
  MPR, and dynamic plugins—now crosses one mandatory fence without destructively capping structured payloads;
  wrapper failure withholds raw bytes. The former injection-defense config/env controls are tombstones.
- Coder execution protections are mandatory and fail closed (#1464): tooling authorization now derives exact
  enabled CLI launch tuples when its boot-only allow-list is omitted, denies explicit-empty/malformed policy,
  and is enforced in Python and Ink launch lanes. Bare logical commands now authorize probe-resolved Windows
  executables by a shared known-extension stem fallback without weakening byte-exact absolute-path pins.
  Mutating tools require a durable audit intent before dispatch and append a result afterward; ledger failure
  blocks pre-action or surfaces latched degraded audit health through refusal until an intent append recovers.
  The former tooling-envelope and audit enable controls are warning-only tombstones. Engine-owned in-process
  reasoning fan-out and CLI-to-local spill remain available; external coder processes are still authorized at
  their actual spawn sites and refused fail-closed on a mismatch.
- Completion authority is always on (#1463): only a selected, readable, non-empty feedback artifact with
  normalized `status: done` can advance a task; all other signals fail closed before egress and transition.
  Local and remote autonomous lanes share the exit-zero compatibility stamp, both client prompts require the
  first-line status contract, and quoted or trailing-punctuation `done` tokens normalize safely. The former
  `advance_gate.enabled` switch is now a warning-only tombstone that cannot be set at runtime.
- Design lifecycle protection is always on (#1462): removed the destructive single-document overwrite and
  approval bypass, made proposal promotion and legacy-vault migration atomic and non-destructive, made
  approved-standard injection fail closed before task/handover writes, and retired `design_gate.enabled` as
  a warning-only tombstone that cannot be set at runtime.
- Client-run task lifecycle (#1455): added idempotent `/claim` and `/unclaim` transitions and wired both
  Python and TypeScript clients to show locally executed work as `in_progress`, release failed runs for
  retry, reject unknown claiming agents, and keep signal transport failures from aborting coder runs.

### Added

- F-B Rust R5 (#1450): consolidated the public docs for the completed Rust egress lane, documenting
  the Python and Rust analyzer matrix, Rust probe limitations, ADR cross-links, and the default-off
  best-effort tripwire framing.
- F-B Rust R4 (#1449): wired Rust dependency, source-scan, and hermetic build probes into the public
  egress dispatch paths with per-ecosystem fail-soft isolation, Rust-only ecosystem-tagged findings,
  unchanged Python-only result shapes, and an engine-side offline Cargo metadata feature resolver that
  reuses the Rust probe-safety gate and neutralized Cargo environment.
- F-B Rust R3 (#1448): added the security-critical two-phase Rust hermetic build probe with
  probe-safe Cargo config gating, neutralized Cargo/Rust environment, ephemeral Cargo state,
  network-allowed `cargo fetch --locked`, sandboxed offline compile-only build/test phases, and
  block-only attribution for clear sandbox network-denial signatures under `network:none`. The
  probe-safe gate rejects legacy `.cargo/config`, credential providers, `rustflags`,
  linker/runner target overrides, path toolchains, unreadable/odd Cargo config, and pins
  `RUSTUP_TOOLCHAIN=stable` during fetch.
- F-B Rust R2 (#1447): added a pure, fail-soft Rust static source egress scanner for advisory-only
  findings on `std::net` imports/calls, known egress-capable crate uses via the shared Rust crate set
  with identifier folding, and literal `Command::new` shell-outs to network tools.
- F-B Rust R1 (#1446): added a pure, fail-soft Cargo dependency-closure egress analyzer
  with a versioned concrete Rust known-egress crate set, feature-gated crate handling via
  injected active feature data, and ecosystem-namespaced allow/deny policy selection.
- F-B S6 (#1439): docs consistency pass for the shipped egress tripwire, correcting stale
  "future", "deferred", and "guidance-only" claims while preserving that `_design_build_check` never
  compares `network`.
- F-B S5 (#1438): added the post-coder advance-time egress enforcement hook. The advance path reads the
  approved design build policy, refuses `severity: block` analyzer findings and restrictive-posture failures
  while leaving the task in progress, and surfaces advisory findings in the advance log (hardened to always-on
  and fail-closed in F3c, #1464).
- F-B S4 (#1437): added an engine-side Python build/test hermeticity runner that discovers pytest and
  import-checkable wheel-build commands, resolves its ADR-0013 sandbox backend independently of
  `security.sandbox`, runs contained probes with network isolation for `network:none`, and degrades
  platform-honestly to advisory findings when no backend can enforce egress containment.
- F-B S3 (#1436): added a pure, fail-soft Python static egress capability scanner for advisory-only
  findings on network imports, raw socket/URL opener calls, literal shell-outs to network tools, and
  best-effort third-party egress import names, with deterministic output and vendor/build directory skips.
- F-B S2 (#1435): added a pure Python dependency-closure egress analyzer with a bundled
  versioned known-egress distribution set. The resolver treats fully pinned requirements files as
  full closure inputs only when every requirement line is a plain pin, falls back to direct manifest
  dependencies when requirements files contain no package names, ships as `ack.egress`, and reports
  blocking versus advisory findings from the S1 egress policy shape.
- F-B S1 (#1434): added a pure, fail-soft `_design_egress_policy(slug)` reader for optional
  `## Build policy` egress lines (`network: none|declared|open`, `allow`, `deny`) on the approved design.
  The reader defaults to open/no lists on missing, unapproved, malformed, oversized, unreadable, or unknown
  policy input and stays separate from `_design_typed` / `_design_build_check`, so `network` is not a
  build-boundary typed comparison.

## [0.0.29]
### Fixed

- Proposals-with-trade-offs S5 follow-up (#1418): `/design --options [N]` now warns when the model records
  fewer proposal files than requested, no longer claims trade-offs were validated in the confirmation text,
  and has negative coverage for fail-closed argument, active-unit, and missing-agent paths.
- Tooling envelope FA follow-up (#1420): DEV-1 now authorizes every configured CLI provider/code-agent
  launch tuple plus the default non-stream autopilot tuple, runtime gates share the same inherited-default
  canonical launch tuple as registry filtering, and Ink treats an absent server-shipped policy as
  default-off to match Python handover parity.
- Constraint reframe follow-up (#1413): fixed the M5 `/fork` proposal list call after the product
  constraint-fork retirement, removed stale ink `/approve constraint`, `/dismiss constraint`, and
  `/fork decide` help/autocomplete surface, and hardened the ink parity guard against stale server verbs
  and retired usage tokens.

### Changed

- Constraint reframe S1 (#1414/#1413): retired the product operator constraint gate, typed HARD-floor
  readers, product constraint-fork ledger, `/approve constraint`, `/dismiss constraint`, and the
  constraint-envelope ACE leg. `record_constraints` now writes optional non-gating framing notes under
  `notes/framing.md`; it no longer writes `decisions/constraints.md` or revokes/blocks approved designs.
- Design lifecycle S3 (#1416/#1413): `record_design` now retains non-destructive
  `proposals/design-<n>.md` variants, `/approve design [<id>]` promotes one approved decision, later
  re-records no longer auto-revoke the approved decision, and `## Build policy` is carried on promote and
  injected with the approved design standard.
- Build enforcement now rests on the approved design standard: `_design_build_check` remains, reads
  `_design_typed`, and uses `ack.ace.constraint_conflict.hardcheck`; `normalize_language` is retained for
  design metadata. The shared `_ACE_FORK_WORKER` and M5 architecture-fork `/fork` proposal surface remain.
- DEV-1 config now leaves `constraint_gate.enabled` and `safety.constraint_conflict_detect` off by default
  for the retired product paths while keeping `design_gate.enabled` and the M5-only
  `ace.fork_mpr.enabled` on.

### Added

- Proposals-with-trade-offs S5 (#1418/#1413): `/design --options [N]` is a deterministic
  operator-triggered design fan-out behind `design_gate.enabled`. It accepts N in the enforced 2..8 range
  and defaults to 2, then asks the model to record N `proposals/design-<n>.md` variants through the existing
  `record_design` path, each with a `## Trade-offs` pros/cons section; the operator promotes the chosen
  proposal with `/approve design <id>`. With the gate off, the command refuses before a model turn or file
  write.
- #1420 completes the ADR-0007 tooling-envelope FA: a default-off
  `security.tooling_envelope.enabled` policy with `{bin, cmd_template}` allow-list loading, the pure
  `ack.tooling_envelope.assert_authorized` helper, strict fail-closed malformed-input handling, exact
  realpath identity for pinned executables, basename-only matching for bare commands, portable `*`/`?`
  globbing, ASCII-whitespace command-template normalization, and shared Python/Ink parity vectors for
  `$VAR`/`${VAR}` plus bare-`~` expansion while leaving undefined env references, `%VAR%`, `~user`, and
  bracket classes literal. Enforcement is wired at every coder-spawn lane: provider CLI fan-out/web-search
  runner, Python handover using the server-shipped policy, autopilot `launch_coder`, reconciler launch
  queue, `review`, `/coders use`, provider/code-agent registry filtering, and the TypeScript handover
  client. `/pending` carries only the non-secret effective allow-list for local client spawn checks,
  envelope-on CLI refusal is terminal instead of spilling to in-engine fan-out, and FA-S3 adds the
  per-lane test matrix plus docs for BYO code-agent operators. Public installs remain byte-identical while
  the policy is off; `GX10_TOOLING_ENVELOPE_ENABLED` mirrors the nested config toggle.

## [0.0.28]
### Fixed

- #1405 fixes constraint-fork resolution so `keep` no longer strands approval on the rejected
  counter design. `ForkEnvelope` now carries the counter design body plus an optional compliant-prior
  snapshot; `keep` clears the rejected counter from `design.md` or restores the prior compliant
  design, while `counter` promotes the counter body into `design.md`. The path remains default-off /
  byte-identical when `safety.constraint_conflict_detect` is off.
- #1405/#1404 hardens the constraint-fork decision edge cases: revalidation-sourced counter
  promotion always writes `design.md` as `approved: false` / `type: proposal`, `keep` reconciles
  `design.md` before marking the envelope resolved, and approved-design revalidation warnings include
  the concrete `/fork decide <fork-id> --choice keep|counter` commands.

### Changed

- #1407 failed/empty code-agent surfacing is always on: a local failed/empty coder run is now marked
  blocked `errored` with its captured log tail, and the clean-stderr client lane can still mark
  `errored` or `unavailable`, instead of leaving the unit silently `in_progress`.
- #1404 surfaces concrete ready-to-run `/fork decide <fork-id> --choice keep|counter` commands with
  option labels wherever a pending constraint fork is shown, so operators no longer have to find and
  type the opaque fork id from a generic placeholder.
- #1400 hardens truncation-finalize accounting: a finalized runaway now silently folds the exhausted
  generation into the same per-turn/session perf counters as the finalizing generation, records successful
  finalize telemetry separately, shows the normal spinner while finalizing, and removes the transient nudge
  by identity.
- #1396 keeps typed constraint body detection best-effort and presence-only while tightening precision: network body signals are now restrictive-only (offline, none, forbidden), so permissive `allowed` / `online` prose is intentionally not gated; language body signals require an implementation verb or an explicit key / `only` / `must` / `requires` / `use` form, so bare `in <language>` / `using <language>` no longer fires; English restrictive network recall now mirrors the German no/without bridge.
- #1396 adds narrow typed-constraint detector recall for `offline-only`, `network is forbidden`, and `Internet verboten`, while applying the existing language object-noun guard to German `verwende` / `nutze` language forms.

### Added

- #1407 adds opt-in boot and launch model validation for code agents via `models_probe` and
  `models_pattern`. The engine compares the configured model against the CLI's advertised list, warns at
  boot, and refuses launches of a cached mismatching model with a named error.
- #1400 adds opt-in `generation.finalize_on_truncation` / `GX10_FINALIZE_ON_TRUNCATION`: a streaming
  reasoning runaway that exhausts the output budget inside `<think>` (`finish_reason=length`, no answer)
  is salvaged into one bounded no-think finalize instead of a blank turn. It fires at most once per turn,
  never persists the transient nudge, and remains default-off / byte-identical.
- #1399 adds default-off resumable partial-persist for post-first-token stream wedges: when the decoupled timeout
  knobs are enabled and the engine watchdog aborts after streamed content has begun, the cleaned partial answer is
  saved so the stalled turn is resumable. Partial tool calls are dropped, unclosed `<think>` content is never
  persisted, and the default public path remains byte-identical.
- #1397 adds default-off LLM timeout decoupling for large-context prefills:
  `connection.connect_timeout_s` / `GX10_LLM_CONNECT_TIMEOUT_S` and
  `connection.first_token_timeout_s` / `GX10_LLM_FIRST_TOKEN_TIMEOUT_S` can keep connection setup short while
  giving time-to-first-token a model-sized budget. The per-turn idle watchdog is phase-aware (generous before
  the first chunk, tight between chunks after that) with a pre-first-token backstop above the httpx read
  deadline, so the named first-token timeout remains reachable; retries no longer re-issue a large-context turn
  after a first-token timeout; and first-token timeout errors name the cause and the config knob to raise.
  Invalid optional timeout values now fail soft to unset, and all knobs are off by default, preserving the
  existing single-float client timeout.
- Added `_constraint_typed_unresolved`, a fail-soft engine reader for typed `source: suggested`
  constraints as the foundation for the #1372 unresolved-constraint gate, plus tightened
  `record_constraints.source` tool guidance for #1369. Gated behavior remains default-off.
- Added the `/dismiss constraint <id|all>` command and the #1370 unresolved-constraint approval
  gate: with `safety.constraint_conflict_detect` on, suggested typed constraints now block
  deviating or omitting `/approve design` calls until the design aligns, the operator dismisses
  the suggestion, or the operator promotes it to HARD and resolves the resulting fork. HARD
  promotion/rewrite paths now revalidate recorded approved designs and revoke contradictory
  approvals. `/dismiss` is an intentional unconditional command-surface addition; gated behavior
  remains default-off.
- Surfaced unresolved suggested typed constraints truthfully in per-turn steering (#1371): the
  constraint state now labels them as advisory, shows `/approve constraint <id|all>` and
  `/dismiss constraint <id|all>` resolution paths, reports design mismatches or omissions, and
  only prints a BLOCKED clause when `safety.constraint_conflict_detect` is enabled.

- **Constraint steering survives context recovery and routes deviations through operator-owned forks**
  (#1362, #1363): with the constraint gate enforced, the authoritative per-turn state now shows the
  normalized HARD typed floor and any recorded design's typed proposal, then directs HARD changes through
  `record_design` → `/fork decide keep|counter`. The orchestrator prompt forbids prose renegotiation and
  silent `record_constraints` replacement; gate-off steering remains byte-identical.
- **HARD constraint overwrite guard** (#1364): when conflict detection is enabled,
  `record_constraints` refuses a model attempt to change or clear an existing typed HARD floor before
  writing. Declared-none captures ignore contradictory typed parameters, suggested captures cannot be
  promoted into the HARD floor by a merge, and operator-confirmed overrides may deliberately clear or
  replace the floor; the default-off path remains byte-identical.
- **L2/L3 constraint E2E capstone** (#1359, epic #1344 S7): real-dispatch integration proof that
  `record_constraints` → conflicting `record_design` → pending `ForkEnvelope` → `/fork list` →
  `/approve design` blocked → `/fork decide` keep|counter → L3 hard-check (refuse-until-rerecord
  on keep; override-and-proceed on counter) → impl `stage_handover` (omission fail-closed; L1
  verbatim injection on success) runs end-to-end with both gates on; both flags off remain
  byte-identical (no fork, no block, no hard-check). Test-only — no engine changes. ADR-0016.
- **L3 structured hard-check at the implementation boundary** (#1342, epic #1344 S6): pure
  `ack.ace.constraint_conflict.hardcheck` (frozen `Violation` with `kind=missing|mismatch`;
  first `TYPED_KEYS` HARD-floor failure wins; omission fails when `require_present`; never
  raises) plus engine `_constraint_hardcheck` gated by default-off
  `safety.constraint_conflict_detect`. Fail-closed at **`/approve design`** (after the S4
  pending-fork block, before `approved: true`), **impl `stage_handover`** (create + re-hand),
  and **`plan_units` children** — compares design/task typed `language`/`network` to
  `_constraint_typed` HARD values. Couples with S4 decide: `keep` → design must match;
  `counter` → overridden floor matches. `record_design` stays advisory (L2 fork only).
  `force` does not bypass. Flag off remains byte-identical. ADR-0016.
- **L2 durable project-scoped fork worker + `/fork` surface + decide→learn** (#1340, epic #1344 S4):
  optional MPR `artifact_slug` port routes `runs_dir`/gate/INDEX to a validated initiative
  (`None` ⇒ byte-identical active binding); when `ace.fork_mpr.enabled` is on, a worker drains
  pending `ForkEnvelope`s with **context captured at submit** (`contextvars.copy_context` +
  `ctx.run` per item — ReflectionWorker is a long-lived daemon). The safe-queue run lock is
  **process-local and non-durable** (in-memory set keyed by `fork_id`, mirrors M5
  `_ACE_FORK_INFLIGHT`) — never a persisted `inflight` claim — so a hard crash re-drains
  `pending` + `recommendation is None` envelopes (#17). Fills `recommendation`/`matrix` under the
  envelope's slug (switch-before-drain + submit-time ProjectContext proof). `/fork` / `/fork list`
  shows pending envelopes (opaque ids, supersession of older same-category); empty M5 fall-through
  stays byte-identical when both flags are off. `/fork decide <fork-id> --choice keep|counter` is
  the R5 state machine (fail-closed, idempotent — keep leaves constraints unchanged, counter
  overrides typed HARD with `operator-override`); `/approve design` (bare `/approve` preserved) is
  blocked while a pending constraint fork exists and **refuses fail-closed** on a ledger-read
  error when `CONSTRAINT_CONFLICT_DETECT` is on; `/approve constraint <id|all> [--slug]` promotes
  suggested→hard. decide→learn feeds ACE from the envelope resolution (fail-soft). Both
  `safety.constraint_conflict_detect` and `ace.fork_mpr.enabled` off remain byte-identical.
  command_spec + Ink autocomplete updated. ADR-0016.
- **L2 structured constraint-conflict detector + durable fork-envelope emission** (#1337, epic
  #1344 S3): pure `ack.ace.constraint_conflict.detect_conflict` (first differing `TYPED_KEYS`
  entry → frozen `Conflict`; never raises) and pure `ack.ace.fork_envelope` (`ForkEnvelope`,
  opaque stable `make_fork_id` excluding free-text `question`, `build_constraint_envelope` with
  keep/counter options). When default-off `safety.constraint_conflict_detect` is on,
  `record_design` compares HARD typed constraints to design typed fields and persists a pending
  envelope under `vault/<slug>/proposals/forks/<fork_id>.json` (atomic, idempotent by `fork_id`;
  fail-soft). Flag off remains byte-identical. Detect+persist only — no MPR run, no `/fork`
  surface, no handover compare/gate (S4/S6). ADR-0016.
- **Typed constraint fields + hard/soft classifier + L2/L3 flags** (#1341, epic #1344 S5):
  pure `ack.ace.constraint_types` allow-list (`language` / `network` normalize + alias-fold,
  never raises); optional typed params on `record_constraints` / `record_design` (invalid →
  `GateRefusal`; omitted → S1-identical frontmatter); `source: hard|suggested` provenance and
  `_constraint_typed` HARD-only reader; optional `TaskSpec.language` / `network` (extra=forbid);
  default-off `safety.constraint_conflict_detect` (`CONSTRAINT_CONFLICT_DETECT`, strict `_as_bool`)
  for L2 detect + L3 hard-check (MPR worker still `ace.fork_mpr.enabled`). S5 plumbing —
  S3 wires detect+persist; S6 wires hard-check. ADR-0016.
- **L1 constraint E2E capstone** (#1343): integration proof that the full L1 flow
  (`record_constraints` → presence-gate → `record_design` → `/approve` → handover
  carries a single verbatim `<!-- IRONCLAD:CONSTRAINTS -->` block) runs through the real
  `run_tool` / `_run_tool_dispatch` path; plus gate-off byte-identical, synthetic
  `_apply_config` activation, and `CAPTURED_NONE` E2E. Test-only — no engine changes.
- **L1 constraint presence-gate + verbatim handover injection** (#1339): when `constraint_gate.enabled` is on,
  `record_design`, `plan_units`, and implementation `stage_handover` (create and re-hand) refuse until the
  active unit has constraints on record (`CAPTURED` or `CAPTURED_NONE`). A single `_constraint_status` snapshot
  drives both the gate and the injection (no TOCTOU). Captured bodies are prepended inside one
  `<!-- IRONCLAD:CONSTRAINTS -->…` block (idempotent strip-then-add on re-hand); `CAPTURED_NONE` strips a
  stale block and injects nothing. Design/analysis handovers stay ungated. `force` does not bypass. Default
  off remains byte-identical.
- **L1 constraint capture** (#1338): adds the deterministic `record_constraints` tool and the single canonical
  `<unit>/decisions/constraints.md` decision, including explicit `declared_none`, reserved-marker refusal, and
  a bounded fail-soft `UNCAPTURED` / `CAPTURED_NONE` / `CAPTURED` reader. The config-only
  `constraint_gate.enabled` flag conditionally exposes the tool and steering status; it defaults off, keeping
  existing flows byte-identical.
- **Cross-model second-opinion `review` tool** (#1221, epic #1212): a dedicated, generic
  `review(focus?, agent?, paths?)` that gets an independent second opinion from **any** configured
  code-agent (KIMI / SONNET / CODEX / OPUS / … — not codex-only) over a working `git diff` (default),
  named files/docs/decisions (`paths`), or any artifact. Mechanism: `_code_agent_registry()` +
  `client.default_cli_runner` (existing synchronous hardened-env CLI runner) — no new backend.
  Capability-detected (`_review_available()` when a reviewer binary resolves); config `review.agent` +
  `review.timeout_s`; SOFT distinct-reviewer anti-affinity (#457) against the producer pin; a READ in
  `_INGESTION_TOOLS` (injection-fenced + char-capped). System-prompt routing: call `review` — never
  self-review.

### Fixed

- **Ink handover stdout isolation** (#1406): locally launched code-agents no longer inherit stdout into
  the terminal owned by the Ink renderer. The client now captures piped stdout, preventing input-box /
  scrollback corruption under `/auto`, writes coder stdout and stderr to a per-task
  `.ironclad/agent/logs/<task>_<agent>.log`, shows only a controlled stderr summary plus short tail
  instead of raw coder output, and uses stdout as the last-resort handover result when a coder emits
  only to stdout.
- **Capture-completeness gate for typed constraints** (#1396): when `constraint_gate.enabled`
  is on, `record_constraints` now refuses fail-closed if DE/EN constraint prose strongly states
  an implementation `language` or `network` requirement but the corresponding typed field is
  omitted. The detector is conservative and presence-only: it emits which category is stated,
  not a detected value, and the refusal asks for that category's typed field without asserting
  `network=none|allowed` or a language value. It still limits language detection to curated
  values (`python` / `rust` / `javascript` / `typescript` / `go`), skips declared-none captures,
  and leaves gate-off behavior byte-identical. German network detection now uses tight
  negation-to-network bridges, widens explicit network recall, and language object-noun
  precision also blocks `written in Go modules`, `using go.mod`, and `using Go-modules` while
  preserving direct requirements such as `written in Go`.
- Polished the suggested typed-constraint copy (#1394): model-facing `record_constraints` prose no
  longer glues parameter help into the top-level tool description, unresolved suggested floors no
  longer over-claim as operator-stated, and dismissed same-value typed categories are covered for
  explicit `source="hard"` re-arming.
- Hardened `record_constraints` field-level provenance merges (#1390, #1391, #1392): an unchanged
  plain re-supply no longer promotes an existing SUGGESTED or dismissed typed category to HARD,
  dismissed typed values survive sibling re-records and can still be re-armed with
  `/approve constraint <cat>`, the `record_constraints.source` copy is conflict-detect-aware,
  and `/dismiss constraint all` now reports that it dismissed typed constraints rather than only
  suggested ones.

- **Suggested constraints re-check approved designs after approval** (#1385): with conflict
  detection on, recording a new SUGGESTED typed floor after a design is already approved now
  appends a non-blocking `WARNING` when that approved design omits or deviates from the suggestion,
  pointing the operator to re-align the design, `/dismiss constraint <id>`, or
  `/approve constraint <id>`. Suggested-only conflicts do not revoke approval; HARD writes keep the
  existing revoke/revalidation behavior.
- **Field-level constraint provenance** (#1379, #1382): typed constraint provenance is now tracked
  per category via `source_language` / `source_network` with legacy doc-level `source` fallback.
  Readers split HARD and SUGGESTED values per category, `/dismiss` and `/approve` stamp only the
  requested typed fields (or every present field for `all`), `/approve constraint <cat>` re-arms a
  dismissed category as HARD, and `record_constraints` allows suggested captures in a different
  category without silently promoting them. `/dismiss constraint all` is a deliberate operator-only
  reset that dismisses every present typed category, including HARD ones; the model cannot invoke it.
- **Constraint approval clears stale realignment forks and truthful suggested-floor copy** (#1383,
  #1384): with conflict detection on, `/approve design` now resolves a pending same-category
  constraint fork when the current recorded design has been realigned to satisfy the HARD typed
  floor, while still refusing when the design continues to conflict. Suggested typed constraint
  steering/tool copy now describes detect-on behavior as gating/protective and detect-off behavior
  as advisory-only.
- **Constraint exception handling is explicit when conflict detection is enabled** (#1380, #1381):
  unresolved suggested-constraint soft-check failures now refuse `/approve design` fail-closed instead
  of silently approving, and approved-design revalidation probe failures now surface a non-fatal
  `WARNING` so promotion and `/fork decide counter` writes remain durable while operators are told to
  re-check manually. Conflict detection off remains byte-identical.
- **Docs: corrected a stale comment that referenced a non-existent `GX10_DESIGN_GATE` env override** (#1347): the design gate is config-only (`design_gate.enabled`); `_env_overrides()` has no such mapping.
- **Gate config flags use strict boolean coercion** (#1346): `design_gate.enabled` and
  `advance_gate.enabled` (plus the same-pattern `*.enabled` / `automation.decoupled` config flags) now go
  through `_as_bool` instead of bare `bool(...)`. A string config value such as `"false"`, `"0"`, or
  `"garbage"` no longer wrongly enables a gate (`bool("false")` is `True` in Python); only JSON `true` or
  an explicit case-insensitive true string (`"true"` / `"1"` / `"yes"` / `"on"`) turns a flag on. Fail-soft
  try/except → False is unchanged.
- **Designs are persisted immediately after research** (#1335): the orchestrator prompt now makes
  `record_design` the mandatory next action after a research or `web_search` phase, and the authoritative
  no-design steering state tells the model to call the tool immediately instead of returning a prose-only
  proposal that cannot arm the gate or be approved.
- **Recorded designs remain proposals until operator approval** (#1336): `record_design` now writes the
  canonical `decisions/design.md` as `type: proposal` with `approved: false`; `/approve` atomically promotes
  it to `type: decision` and `approved: true`. The gate remains fail-closed on `approved`, so legacy
  unapproved decisions stay pending and self-correct on the next record or approval without a destructive
  migration pass. The refusal now directs operators only to `/approve`, preventing a manual flag edit from
  leaving inconsistent frontmatter.
- **Forced task creation never duplicates an exact title** (#1330): `force=true` now bypasses only fuzzy
  topic matching; exact normalized-title matches remain fail-closed and direct handovers back to the existing
  task ID instead of suggesting force.
- **Coder handovers identify the configured code root** (#1328): when `paths.code_subdir` is active, the
  engine now injects an idempotent handover note that tells the coder its working directory is already the
  code root, preventing an extra subdirectory prefix from producing `src/src`-style source trees.
- **Orchestrator task-type vocabulary matches the ACK contract** (#1329): the planning and handover
  prompt now uses `optimization` for performance work and labels scaffolding as `implementation`, while a
  parity test prevents prompt task-type values from drifting beyond `ack.case_spec.TaskType` again.
- **Guided mode recommends the launch instead of auto-starting the coder** (#1327): the orchestrator
  system prompt instructed the model to call `launch_coder` itself in guided mode (`/auto off`),
  contradicting the documented `off = guided mode: engine recommends, operator drives` semantics (already
  reflected in the status matrix). Guided mode now STOPs after staging the handover and recommends the
  launch to the operator, calling `launch_coder` only on explicit operator instruction; `/auto on`
  (the harness launches) is unchanged and `launch_coder` stays an operator-triggerable tool.
- **`review` config-default never self-reviews when a peer exists** (#1221): `_pick_reviewer` applied
  anti-affinity only after the config `review.agent` short-circuit, so a config equal to the producer pin
  could return a self-review while a peer was runnable. Config-default / no-arg now prefers a runnable
  peer in that case (SOFT waive only when the producer is sole runnable); an explicit `agent` arg stays
  honored. The tool's model-facing `agent` enum is also LIVE from `_code_agent_registry()` (via
  `_tools_with_agent_enum`), not hard-coded OPUS/SONNET.
- **Docs: Memory MCP launch semantics.** The Memory MCP docs and server/client comments now reflect the
  #994-S10 always-on semantics when a memory service is configured and the agent ships an `mcp_template`,
  instead of the retired sealed-gating claim.
- **Coder logs are live and abort-survivable.** The server-side `_do_launch` path now captures coder stdout
  through a line-drained pipe and a write-through logfile reader instead of handing the child a block-buffered
  file handle, so `<task>_<agent>.log` can be tailed during the run and retains flushed partial output after
  a kill, timeout, crash, or interrupt. The parent decodes stdout as UTF-8 with replacement characters, so
  invalid bytes cannot kill the drainer and wedge the child behind a full pipe. Follow-up scope:
  `claude --print` is still the current log contract; switching to `--output-format stream-json` is a
  separate format and feedback-parser change.
- **ACK gx10 test isolation no longer leaks config/runtime state.** The autouse fixture now snapshots the
  gx10 engine globals rebounded by `_apply_config`, restores mutable registries between tests, and keeps ACE
  lifecycle globals out of the snapshot path so live workers are stopped and hard-cleared, preventing
  order-dependent #1298 pollution.
- **Watcher automation now hangs off `/auto`.** The feedback watcher defaults OFF at boot, `/auto on` enables
  it with autopilot and continuation, and `/auto off` disables it again. The old `/watcher on|off` command is
  kept only as a compatibility alias for the same `/auto` paths, so `/health` no longer reports
  `watcher:true` before autonomous mode is armed.
- **The orchestrator's own file/command tools now run in the active project.** With a client offering local
  tools (`code_locality=mount`), the orchestrator's bridged `read_file`/`write_file`/`execute_command`/… ran
  in the client's frozen startup directory (the boot workdir), not the active project — so after an in-session
  project switch the agent's own tools targeted the wrong tree. And `edit_file` (the one code-tool that ran
  server-side) resolved the same relative path to a different file, so it could report `OK: edited` without
  the change landing. The server now ships the active project's exec cwd in each tool-bridge frame; both
  clients run the tool there (falling back to their own directory when that path is absent — remote / older
  engine); and `edit_file` refuses a no-op edit (`old_string == new_string` / already applied) instead of a
  false success.
- **Handover body names the right coder.** The frontmatter `to:` (the resolved agent, and the handover
  filename) is authoritative, but the model authors the free-form body Meta block and could name a
  different agent there (e.g. `Recipient: CODEX` on a Sonnet handover) — confusing for the coder that
  reads it. The engine now rewrites the body `Recipient:` line to the resolved agent, so the body agrees
  with the frontmatter.
- **Decomposed units now carry their build order.** The engine already selects units in dependency order
  (a unit runs only once its `dependencies` are done, priority breaking ties among ready units), but a
  small orchestrator model tended to leave `dependencies` empty — so `plan_units` produced a plan the
  selector ordered by priority, mis-ordering the build (e.g. a module before its scaffolding). The
  decomposition prompt now guides declaring the real build-order edges (`unit:<n>` — scaffolding before
  modules, a module after the models/utils it imports, tests after the code), and `plan_units` appends a
  steering note when a multi-unit plan declares no dependencies at all.
- **No more double-driving the coder under `/auto`.** With autonomous mode on, the loop launches staged
  handovers itself (the client polls `/pending`), yet `plan_units` still told the orchestrator to author the
  first unit's handover — and the orchestrator would also call `launch_coder`, a second launcher racing the
  loop for the single coder slot (a confusing `BUSY` collision + a contradictory "already running" message).
  `launch_coder` now **defers with a clear no-op when `/auto` owns launching** (it launches only in guided
  mode, `/auto` off), and the `plan_units` armed-automation prompt tells the orchestrator explicitly not to
  call `launch_coder`.
- **Coder project isolation on the local/desktop topology.** A code-agent launched by the client (the
  `/pending` poll behind `/work` and `/auto`) ran in the client's static startup working directory, so
  after an in-session project switch a coder could build one project's code into another project's tree —
  the client never re-synced its working directory to the active project. The server now ships the active
  project's execution root (its exec cwd = `<project-root>/<code_subdir>`) in each `/pending` item, and the
  client launches the coder THERE; the agent scratch (handover in / feedback out) stays client-local via
  absolute paths, so the feedback round-trip is independent of the coder's working directory. Falls back to
  the client's own directory when the server ships no cwd (older engine) — byte-identical.
- **Autonomous coder can now run the tests it writes.** The headless code-agent launched by the client ran
  with `--permission-mode acceptEdits`, which auto-accepts file edits but **not** commands — so in a
  non-interactive `--print` run every `python`/`pytest` invocation was silently denied and the coder could
  never self-verify. The coder default is now `bypassPermissions` (parity with the server-side autopilot
  launch, which already uses `--dangerously-skip-permissions`), so the full write-and-run-the-test loop
  works out of the box. Set `GX10_CLAUDE_PERMISSION_MODE=acceptEdits` to restrict a coder to edits only.

## [0.0.27]
### Added
- **Design-driven autonomous continuation — an approved design now drains to done end-to-end.** Previously a
  design-driven project stopped after its first task: the post-advance planner could only continue from a
  configured capability backlog, and with none it silently disarmed itself — nothing ever staged the next
  unit. Three pieces close the loop:
  - **`plan_units` (new macro)**: after design approval, ONE call materializes the FULL decomposition — one
    `epic` task (new ACK `TaskType.EPIC`) plus ALL implementation units as pending tasks linked via `parent`,
    deliberately handover-less (each unit's handover is authored lazily when the loop selects it). Atomic +
    fail-closed (per-unit ACK validation, topic dedup incl. within the batch, full rollback on error);
    `epic_id` adds units to an existing open epic; in-batch sibling dependencies as `unit:<n>`. The engine
    auto-completes the epic when its last unit advances; the board shows per-epic unit progress.
  - **Select-next-unit continuation** (`_continuation_tick`): after every advance the engine deterministically
    selects the next open unit (priority → created_at → id; skips blocked; dependencies must be done — a
    deadlock is surfaced, never a silent idle) and asks the model for exactly that unit's handover
    (`[NEXT-UNIT]` turn → `stage_handover` with `task_id`); with no open units the capability-backlog leg
    continues as before; with no source the loop idles ARMED (no more self-disable — only the `max_tasks`
    cap stops it). Arming (`/auto on`, `/autoplan on`) bootstraps the loop for the FIRST unit of a freshly
    planned epic (no predecessor advance exists yet), and `plan_units` under an armed loop has the
    same turn author the first handover — found live in the E2E acceptance run.
  - **`/auto on [N]` / `/auto off` (automation meta-switch)**: one operator verb for the whole loop — full
    automation (watcher + autopilot + continuation, optional task cap) vs guided mode (nothing fires by
    itself; the engine recommends the selected next unit in the per-turn steering state and the operator
    drives). The granular toggles remain as the advanced layer; in the recommended client `/auto` also
    drives the local handover poller.
### Changed
- **Client agent scratch is cleaned after a successful upload**: the terminal client's HTTP-mediated
  handover round-trip materializes per-task scratch under `<codedir>/.ironclad/agent/` (the handover
  drop the coder reads + the feedback/capture files). These accumulated per task; after a successful
  `POST /feedback` they are now removed (fail-soft). A FAILED run keeps its scratch for diagnosis and
  the retry; the server-side `.work/archive/` history remains the durable record.
- **Lazily staged handovers get full parity**: the `stage_handover` re-hand path (existing `task_id`, no
  task_json — the continuation's staging form) now applies the same id normalization, Memory brief and
  lesson/ACE context injection as task creation, routes the coder deterministically off the STORED task,
  and stamps the staged agent as `assigned_to` (canonical identity: filename == assigned_to == body `to:`).
- **active.md projection with handover-less units**: the projection now walks newest-first to the first task
  that actually HAS a handover, so a staged handover is never shadowed into `idle` by a newer, not-yet-staged
  unit of the decomposition.
- **Cost warning tells the truth**: the unbounded-continuation warning now names the real cost — every
  continued unit launches a PAID coder run (the local planner turn is the cheap part) — and recommends a cap
  (`auto on N` / `autoplan on N`).
- **Deterministic, cost-aware coder routing** (#1287): the coder for a handover was the orchestrator model's
  pick, which defaulted to the priciest coder for everything — routine scaffolding and running a build ran on
  the most expensive model even at medium effort. Coder selection is now DETERMINISTIC: each task TYPE maps to
  a cost TIER (`complex`/`standard`/`routine`/`analysis`) and `stage_handover` routes to the CHEAPEST CAPABLE
  coder for that tier (`_route_code_agent`, by `cost_per_1k`), reserving the top-tier coder for `complex`
  (security/architecture/optimization). The operator pin still overrides. Reverses the 2026-06-25 "staged pick
  is authoritative" rule.

### Fixed
- **Terminal client froze permanently on every confirm/guide reply**: the destructive-command
  warning ("re-run with --yes") and the guided-input listing returned early WITHOUT leaving the
  thinking state — every subsequent keystroke was swallowed and Esc had nothing left to abort, so
  the session was wedged for good. Both early-return branches now release the turn state; a
  regression test drives the real component against a stub engine and types after the warning.
- **Single-authority completion (presence-wins)** — a follow-up to the dev-loop stabilization, from a comparison
  with the *proven-stable* predecessor gx10 loop of the: it decided completion by the
  feedback FILE'S PRESENCE, never by parsing model-authored content. The advance gate now does the same — the
  `status:` token is ADVISORY (it HOLDS a finished task only on an EXPLICIT `blocked`/`clarification_needed`);
  a present feedback with a done / mis-placed / absent token ADVANCES. This deletes the whole stall class
  where a bare leading `status:` (vs a frontmatter parser) or a prose-only capture defeated a content parse,
  keeping the explicit-blocked guard. Principle: every engine-owned fact has ONE authoritative source.
- **Autonomous dev-loop stabilization** — engine-owned robustness so a completed coder run reliably advances
  the pipeline (fixes THE STALL surfaced during a live run, plus #1288/#1291/#1292 follow-ups). One unifying
  principle: engine-owned facts (completion STATUS, routed AGENT, feedback PATH, PROJECT, CODE ROOT) are
  stamped/read by the engine, never round-tripped through model-authored free text. (1) The advance gate reads
  the completion `status:` tolerantly (`_feedback_status` — an in-frontmatter OR a bare leading line, via a
  bounded head-scan) matching exactly what the engine's own coder prompt emits (a bare leading `status: done`
  used to be invisible to the frontmatter parser → the completed task stalled forever); a capture-mode coder's
  exit-0 non-empty feedback with no status token is stamped `status: done` at ingest. (2) A gate refusal is
  now a RETRY point — the reconciler dedup keys on the feedback mtime — not a dead-end until restart. (3) The
  routed agent is stamped as the single canonical identity (`assigned_to` + handover `to:`) so filename /
  assigned_to / body / feedback `from:` agree. (4) The advance matches the feedback by TASK ID (glob),
  deriving the agent from the filename, never a caller-supplied (routing-skewed) agent. (5) The coder prompt
  states the project name + code root and forbids a design-named wrapper directory (no `src/<name>/<name>/`).
- **Autonomous pipeline no longer stalls on a completed Claude coder run** (#1288): a finished OPUS/SONNET
  (`claude --print`) coder wrote its feedback under the handover body's own id/location (e.g. a divergent
  `<other-id>-feedback.md` in `.work/handovers/`), while the reconciler advances only on
  `{task_id}_{agent}-feedback.md` in `feedback_dir()` — so the task stayed `in_progress` forever despite a
  `status: done` result. The engine now states the exact feedback path (and the `status:` contract) in the
  Claude coder prompt, mirroring the CODEX `-o {feedback}` capture, so a completed run lands where the
  reconciler looks and the pipeline advances.
- **Design-doc paths are shown in the operator's frame** (#1276): `record_design`, `/approve` and the design-
  gate status reported a vault-root-relative path (`<slug>/decisions/design.md`) that did not resolve from the
  project root (the leading `vault/` was missing). They now surface a navigable project-root-relative path
  (`vault/<slug>/decisions/design.md`); the internal value the gate resolves against is unchanged. `/project
  new` also now seeds the single software unit under a canonical `main` slug (not the project name), so the
  doc no longer double-nests as `<project>/vault/<project>/…` — it is `<project>/vault/main/…`.
- **Tool calls fold in the LIVE streaming preview, not only at commit** (#1277): the terminal client rendered
  the streaming answer as raw text (tool calls shown expanded) and only folded them into collapsed blocks when
  the turn committed. The live preview now renders through the same `splitToolBlocks` path as the committed
  turn, so a tool call is collapsed while it streams.
- **The terminal client echoes a slash-command WITH its leading slash** (#1278): a typed `/work` was echoed as
  `> work` — identical to a chat message — inviting a `work` vs `/work` mix-up. The echo (and the persisted
  transcript) now show the original input, so a command is visibly distinct from a plain message.
- **Code-agent handovers ship the RESOLVED bin path on `/pending`** (#1279): `/pending` shipped the LOGICAL
  `bin` (e.g. `codex`) instead of the boot-probe-resolved executable path, so the terminal client spawned a
  bare `codex` and node's PATH could pick the wrong install (an npm shim / a Store desktop app) that rejects
  `exec -m` — the coder exited without producing code. `/pending` now ships the resolved path the boot probe
  found (the exact one `/coders` reports), falling back to the logical `bin` only when unresolved.
- **Destructive-command confirmation accepts `--yes`/`--confirm` in any position** (#1281): the confirmation
  was only recognised as a TRAILING token, so `project delete X --yes --purge` (a flag after `--yes`) re-fired
  the confirm gate ("Re-run with --yes") even though the operator had already confirmed. `--yes`/`--confirm`
  is now stripped as a standalone token from anywhere in the message.
- **Code-agent handovers no longer ship the agent id as the model on `/pending`** (#1279): the `/pending`
  serialization read the handover's `to:` (the recipient AGENT, e.g. `to: CODEX`) into the `model` field, so
  the terminal client rendered `-m CODEX` and a non-Claude coder CLI exited immediately (`unknown option '-m'`
  / model-not-supported — no code produced). The #1236 guard that already dropped an agent-id-as-model in
  `_do_launch` is now applied on the `/pending` path too, so `spec.model` (the agent's real model) wins; a
  genuine model string in `to:` still overrides.
- **Deprecated `/initiative` alias no longer advertised** (#1264): `/initiative` (the alias for `/project`)
  stays dispatchable for back-compat but is now hidden from `GET /catalogue` and the terminal client's
  slash-autocomplete. A single `is_deprecated` convention (a verb whose spec summary says so) is shared by the
  model-facing command surface and the client-facing catalogue; the ink client marks the entry `hidden` and
  drops it from completions while keeping it in the parity registry.
- **No duplicate top-level heading in recorded design docs** (#1267): `record_design` injects `# {title}` only
  when the body does not already open with its own H1, so `decisions/design.md` no longer starts with two `#`
  headings (the frontmatter `title:` stays canonical either way).
- **`/approve` recommends the next step** (#1269): after approving a design, the confirmation now points to the
  concrete continuation (break the design into implementation tasks / stage the first handover), mirroring the
  guided recommendation `record_design` already emits — instead of leaving the operator at a dead end.
- **Streaming no longer shows raw `<tool_call>` markup** (#1266): when the model emits a tool call as text
  (`<tool_call>…</tool_call>` — e.g. a reasoning turn where the endpoint returns no native `tool_calls`), the
  live stream filter now suppresses that block like `<think>`: `_ThinkFilter` is parameterized and the chat
  stream chains a second instance for `<tool_call>`. The RAW content still feeds the post-turn text→tool_call
  recovery, so the call still executes and folds in; a one-time "⋯ tool call" hint replaces the raw JSON.
- **English `ironclad:index:auto` marker + in-place migration** (#1265): the auto-managed `INDEX.md` block
  marker is now English and description-less, consistent with the `board`/`lifecycle`/`related` markers (it was
  a frozen German string). `reconcile_vault` normalizes any prior `ironclad:index:auto START …` marker to the
  current one before rewriting the block, so an existing vault's managed block is migrated **in place**, never
  duplicated (idempotent). The export is fully English here now — the english-only allowlist no longer needs a
  marker exception.
- **Autonomous mode reports an empty pipeline instead of a silent no-op** (#1268): enabling `autopilot on` /
  `autoplan on` when nothing is queued now prints a one-time hint — the pipeline is empty, seed the first unit
  (break the approved design into implementation tasks / stage a handover), and (when no
  `paths.active_capability_backlog` is configured) that autoplan cannot bootstrap one either. autoplan is a
  post-advance continuation from a capability backlog, not a bootstrap from an approved design, so on a fresh
  project it otherwise did nothing with no feedback.
- **`/project delete` no longer freezes the engine + client; a vanished active project can't reappear** (#1263):
  the memory-scope forget on delete now runs on a background thread — a synchronous remote `/delete_all` (up to
  the memory client's timeout, over LAN) used to freeze the whole single-driver engine (and the client, which
  has no own timeout) for as long as the call hung. The registry removal stays synchronous + authoritative; any
  residual partition is swept by the S15 orphan-GC. Separately, `init_registry` now falls back to `default`
  (with a warning) when the active project's root has vanished out-of-band, instead of silently re-scaffolding +
  re-binding it — so a project the operator deleted does not reappear as an empty dir after a reboot.

## [0.0.26]
### Added
- **Grok (xAI) documented as a headless code-agent CLI** (#1246): a `docs/code-agents.md` example for the
  stdout-capture variant (drops `{feedback}`, `-m grok-build`, `--yolo`), complementing the Codex
  `-o {feedback}` example. The engine's agent registry is already agent-agnostic, so onboarding a coder is a
  configuration-only addition (the handover pool + the reasoning fan-out pool) — no engine change.
- **Watcher/autopilot disentangle + task heartbeat + explicit blocked state** (#1229): three opt-in,
  default-off mechanisms (byte-identical when off). `automation.decoupled` makes autopilot self-sufficient (the
  reconciler runs if either concern is on; the feedback-advance side stays watcher-gated) and drops the
  contradictory "watcher on required" double message. `heartbeat.stall_seconds` flags an in_progress task that
  shows no progress signal (coder log / feedback mtime) for N seconds. A task-level BLOCKED annotation
  (`mark_blocked`/`clear_blocked` — no 4th directory state) surfaces a refused (advance-gate) or stalled task
  with a ⚠ marker on the board instead of a healthy-looking in_progress row.
- **Basic no-blind-advance gate** (#1224): opt-in (`advance_gate.enabled`, default off → byte-identical), a
  deterministic pre-`done` gate — an advance to done requires the feedback to declare `status: done`;
  `blocked`/`clarification_needed`/no-status is refused ("no signal ≠ done", fail-closed). Reads the feedback
  only when the gate is on. The full composed gate (coupling/CI/quality) is out of scope.
- **Central task board — `/board` + BOARD.md** (#1228): a human-readable, LLM-free projection of the TaskStore
  — every unit grouped pending/in_progress/done (`id` · type · title · labels · parent · created_at) — so the
  operator sees the whole pipeline at a glance. `/board [slug]` renders it and writes `<unit>/BOARD.md`; the
  file is kept current via the fail-soft soft-reconcile (no new hot-path failure) and is excluded from the
  vault index/graph. Deterministic + timestamp-free (idempotent re-render).
- **GitHub-issue-shaped unit data model** (#1223): a work unit now carries optional `labels` + `parent` (the
  epic/sub-issue link) alongside its title/description/state, so a unit maps 1:1 onto a (sub-)issue. Additive +
  optional (empty defaults → byte-identical when unused); the `model_json_schema()` SSOT propagates the fields
  to the prompt, the constraint grammar, the validator, and the docs at once.
- **Design→implementation approval gate — no blind coding** (#1227): the orchestrator can no longer jump from
  an analysis straight to a coding handover. `record_design` persists the design as a `decisions/` doc
  (`stage: design`, `approved: false`); the engine then **refuses an implementation `stage_handover`** until
  the operator approves it via **`/approve`** — a fail-closed pre-code
  gate (`force` does not bypass it) — opt-in via `design_gate.enabled` (default off; byte-identical when off, DEV-1 enables it). Design/analysis/documentation handovers are unaffected. The per-turn
  steering-state block surfaces the gate state so the model knows why a handover would be refused.
- **`launch_coder` — the orchestrator starts a staged handover on demand** (#1226): the model is the single
  steering author, so it triggers the coding session itself instead of waiting for a daemon. `launch_coder`
  resolves the newest pending task with a staged handover (or an explicit `task_id`), launches its agent via
  the same machinery the reconciler uses, and flips the task to in_progress — WITHOUT enabling the autopilot
  daemon (which stays off by default: no second steering authority). Fail-closed (nothing staged · already
  running/done · no agent on this box → the coder runs on the client in the server topology · concurrency cap
  reached), never double-launches, and never leaks a concurrency slot on a launch error.
- **Per-turn authoritative steering-state injection** (#1225): the orchestrator now sees its own bound state —
  active project · active unit · lifecycle stage · N pending/M in_progress tasks · watcher/autopilot flags —
  folded onto each user turn from the SAME state the plumbing acts on, so it never GUESSES (the prior failure
  mode: probing the filesystem, concluding "no active project" while one was active, then fabricating a path).
  Kept as EXACTLY ONE current copy (stale copies dropped each turn) after the stable system prefix
  (KV-cache-safe); `""` when nothing is bound → a plain-chat turn stays byte-identical. Never raises.
- **`pr_status` — read a PR's CI/mergeability snapshot** (#1219): the merge-readiness gate as a first-class
  read. `pr_status(number)` returns a deterministic per-check verdict (ALL PASSING / N FAILING / N PENDING)
  plus `mergeable`/`mergeStateStatus`/`reviewDecision`, on both transports (`cli` = `gh pr checks` +
  `gh pr view`, `native` = `GET /pulls/{n}` + the head commit's check-runs (paginated) + legacy commit statuses + a best-effort reviewDecision from `/reviews`). A **snapshot** — non-blocking,
  never a `--watch`/poll (the engine runs one agent turn behind a single lock; re-poll across turns). It is a
  read (in the ingestion char-cap + injection fence, not the audit ledger); a non-existent number returns an
  authoritative `NOT_FOUND`. Note: `gh pr checks` exits non-zero as *data* (pending=8, fail=1) — the cli
  adapter parses stdout first rather than treating the exit code as an error.
- **`comment_on_issue` — append a comment to a tracker issue through the forge adapter** (#1217): the third
  leg of create/read/**comment** on the forge seam. `comment_on_issue(number, body_file)` runs on both
  transports (`cli` = `gh issue comment`, `native` = `POST /issues/{n}/comments`); escape-free (body from a
  file), **narrow — comment-only** (never closes, reopens, or relabels — close is policy-sensitive and
  excluded). A non-existent number returns an authoritative `NOT_FOUND` (repo-disambiguated on the native
  path). Capability-detected + `sealed`-blocked + audited, like `create_issue`.
- **`create_pr` — open a PR through the forge adapter** (#1215): the WRITE-sibling of `create_issue` on the
  forge seam, so the orchestrator's Issue→Branch→**PR**→Merge loop no longer drops to the raw shell.
  `create_pr(title, body_file, base?, head?, draft?)` runs on both transports (`cli` = `gh pr create`,
  `native` = `POST /pulls`); escape-free (body from a file), **open-only** (it never merges — merge stays a
  CI/review gate). Capability-detected + `sealed`-blocked like `create_issue`, and audited. The native path
  requires an explicit `head` (no local git to infer it) and defaults `base` to the repo's default branch.
- **Forge adapter seam — the forge tools are now `gh`-independent** (#1213): a vendor-neutral `forge.adapter`
  seam (`cli` \| `native` \| `mock`), mirroring the `web_search` adapter. `cli` (default) is the ambient
  `gh` CLI, **byte-identical** to before; `native` is a stdlib-`urllib` GitHub REST client so
  `create_issue`/`view_issue` (and the follow-up PR/comment tools) work with **no `gh` CLI on the box** — the
  capability is general in ironclad, not contingent on a `gh` binary. The native token is read
  **name-indirectly** from the environment (`forge.token_env`, default `GX10_FORGE_TOKEN`; never a secret
  literal in core), the repo comes from `forge.repo`, and requests are SSRF-guarded to `api.github.com`.
  `_forge_available()` is now transport-aware (offered when `gh` is present **or** a native token+repo is
  configured); still `forge.enabled`-gated and blocked under the `sealed` profile.
- **`view_issue` — read a tracker issue, the first-class way to resolve a `#NNN` reference** (epic #1043):
  the read counterpart to `create_issue`. `view_issue(number)` queries the code forge (GitHub via the `gh`
  CLI) and returns the issue's number/state/title/labels/milestone/body/url; a non-existent number returns an
  authoritative `NOT_FOUND` (the tracker **was** queried). This gives the orchestrator the correct path for
  "check #NNN" instead of falling back to the generic shell and grepping git history — which only ever cites
  issues a merged PR closed, so an open issue is invisible there, making "no commit found" a false
  "issue does not exist". Capability-detected + trust-gated exactly like `create_issue` (offered together
  when `gh` is present and the profile permits; blocked under `sealed`; `forge.enabled=false` forces both
  off; body bounded so a huge issue can't blow the window). The default orchestrator system prompt now routes
  a `#NNN` reference to `view_issue` and forbids concluding "issue does not exist" from a missing commit.
- **Guard 1 — a turn can no longer silently stall the orchestrator** (epic #1130 / #1131): three fail-soft
  bounds so a wedged/stalled turn can't hold the server agent lock forever with no output. (1) A per-request
  LLM timeout on **every** OpenAI client (the agent client + the ACE reflector; the reasoning workers and MPR
  reuse the agent's), so a hung completion fails soft instead of hanging for the SDK default (~600s) × retries
  — `connection.request_timeout_s` / `GX10_LLM_TIMEOUT_S` (default 120s), `connection.max_retries`. (2) The
  tool loop now honours cancellation between tool calls and answers **every** pending `tool_call` on abort, so
  it never leaves an orphan `tool_calls` message (a hard vLLM 400). (3) A bounded agent-lock acquire in the
  server: a second request behind a running/wedged turn gets a retryable **503 "busy"** instead of blocking
  forever — `GX10_AGENT_LOCK_TIMEOUT_S` (default 45s).
- **Guard 1 watchdog — a stalled turn is now aborted AND surfaced, never silent** (epic #1130 / #1132): a
  per-turn IDLE watchdog resets on every progress signal (a generation chunk, a completed generation, a tool
  result) — so a slow-but-progressing deep turn (e.g. an MPR panel) is never killed — and, if a turn makes no
  progress for `context.turn_idle_timeout_s` / `GX10_TURN_IDLE_TIMEOUT_S` (default 240s), aborts it and renders
  a distinct `stalled` turn-end marker ("⏱ TURN ABORTED — model stalled"). The operator is never left staring
  at a silent, wedged turn. Disabled (`<=0`) is byte-identical.
- **Coloured directory listings** (epic #1144 / #1196): the listing default is now
  `ls -lA --color=always`, whose ANSI SGR colours the entry names like a native terminal `ls`. The engine
  splits the two audiences at the run-loop choke point: the **model context** is ANSI-**stripped**
  (`_strip_ansi`, scoped to `execute_command` so ingested file content is never altered — the model reads
  clean text and the ingestion cap counts real characters, not escape bytes), while the **display** stream
  keeps the raw colour. A coloured line is streamed **without** the default grey wrap, so its
  `⎿`/continuation prefix stays plain at the start and the client's tool-block parser captures the line WITH
  its inner SGR (raw-or-stripped match) for the renderer to paint. The renderer already sandboxes SGR (colour
  only — non-SGR escapes are dropped, never reaching the real terminal), so a hostile filename can tint text
  but cannot hijack the cursor. On a non-coreutils host (BSD/macOS `ls`, which rejects `--color=always`) the
  listing is retried without the flag so its header/`Answer:` are never lost. This supersedes the earlier
  muted-palette choice for tool output: native shell colours (ls / PowerShell) are now preserved in the fold.

### Changed
- **SONNET code-agent default upgraded to Claude Sonnet 5** (#1258): the built-in SONNET handover agent now
  defaults to `claude-sonnet-5` (was `claude-sonnet-4-6`); OPUS (`claude-opus-4-8`) is unchanged. A deployment
  can still override the model per agent in its config pool.
- **CI runs a single Python version (3.14)** (#1231): the per-PR `test` job dropped the `3.10 + 3.12` matrix
  for one `test (3.14)` leg, and the branch-protection required-checks SSOT follows (`test (3.10)`/`test (3.12)`
  → `test (3.14)`). `requires-python` stays `>=3.10`, so install-compatibility is unchanged and broad — but the
  3.10–3.13 floor is no longer exercised per-PR (a deliberate trade-off; move it to a periodic run if that
  coverage is wanted again).

### Fixed
- **`ironclad` reuses a running engine only for the SAME project** (#1252): the launcher compared only the
  engine version, so a running engine on the shared port (default 8100) bound to a DIFFERENT project's workdir
  was silently reused — the client then talked to the wrong project's vault/registry. It now also compares the
  engine's `/health` `workdir` against the current folder and restarts the engine on a mismatch (`.sh` + `.ps1`).
- **`/project list` (and initiative/track lists) mark the active row consistently** (#1238): the active row
  used a leading `* ` marker that the client's markdown renderer turned into a generic `- ` bullet, dropping the
  active marker its own `(* = active)` legend advertised. The lists now use a markdown-safe `[active]` tag (the
  same `[…]` convention as `[archived]`) on a clean `- ` bullet, so the active unit is visually distinguishable.
- **The developed software is isolated in a code subdir** (#1237): with `paths.code_subdir` set (opt-in,
  default off → byte-identical), model-driven execution (code-tools, `execute_command`, the launched coder —
  all via `_exec_cwd`) runs under `<project>/<code_subdir>` (e.g. `src/`), so the product tree no longer lands
  alongside the ironclad control-plane (`vault/`, `.ironclad/`, `tasks/`). The control-plane keeps resolving to
  the project root.
- **`ironclad` in a new project folder no longer demands a re-install** (#1232): the installer now records the
  runtime once at `~/.ironclad/runtime.json`, and the launcher **auto-binds** a new folder to it (mints a local
  `.ironclad/config.json`) instead of dead-ending with "run ironclad-install". Separates the one-time runtime
  install from the per-directory project bind; a project's own `config.json` still wins. (`.sh` + `.ps1`)
- **A launched coder no longer gets `--model <AGENT>`** (#1236): the handover frontmatter `to:` is the
  recipient AGENT (e.g. `to: CODEX`), which the orchestrator writes there — it was read as a model override,
  so a non-Claude agent launched with `--model CODEX` and crashed ("the 'CODEX' model is not supported"). An
  agent-name in `to:` is now ignored for the model (the agent's configured model wins); a genuine model string
  in `to:` still overrides.
- **`/update` works out of the box** (#1243): the desktop installer now stamps `srcDir` (the repo root) into
  the project config, so `/update [pull]` (rebuild + reinstall the client from source) no longer fails with
  "needs the source path — set GX10_SRC" on a fresh install.
- **Listings default to `ls -lA` so the visible rows match the count** (epic #1144 / #1199): the default
  listing was `ls -la`, which prints the `.`/`..` pseudo-entries as two extra `d` rows — a directory of 19
  real subdirectories showed 21 `d` lines, so an operator counting rows read the (correct) `19 directories`
  header as wrong. The default is now `ls -lA`: hidden entries are still shown, but `.`/`..` are not, so the
  rows a user can count equal the deterministic header + `Answer:` sentence. Detection is unaffected (`-A`
  is not recursive); an explicit `-R` still suppresses the header.
- **The listing reply itself is now built in code — the model copies, it never composes** (epic #1144 /
  #1202): the one-prose-sentence summary format was still only a prompt rule, and the model kept violating it
  stochastically (blocks instead of a sentence) and even dropped names while enumerating (18 dir names under
  a correctly copied count of 19). A simple shell listing now carries a machine **`AnswerData:` payload**
  directly under the count header (ONE filesystem snapshot feeds both — no self-contradicting pair), and the
  SERVER renders it into the final ready-made **`Answer:` sentence** — **command-gated inside `run_tool`
  itself** (`_localize_listing_answer`), so a SINGLE structural site covers every topology AND every caller
  (the model run-loop, `/tool`, `/ls`, the API): the machine `AnswerData:` line never leaks to a user, and a
  NON-listing command whose output merely mimics the shape (e.g. `cat` of a crafted file) is never rewritten.
  The sentence is `_listing_answer_sentence`: ALL names case-insensitively sorted, backtick-wrapped for
  deterministic inline-code colouring, **sanitized against filename injection** (backticks + every
  `str.splitlines()` line/paragraph separator — LF, NEL, LS, PS — render as `?`, so a hostile name can neither
  forge a line nor break a code span in the verbatim-copied reply), en/de templates with English fallback,
  and robust to malformed/type-confused data (the machine line is dropped, never a fabricated one). Listing
  DETECTION is hardened too: recursion is caught case-insensitively (`gci -recurse`, not just `-R`) and a
  PowerShell cmdlet with any value-taking named parameter (`-Exclude X`) yields no header (no guess).
  Language, templates and sorting live ONLY server-side — the Ink tool bridge (`listingCount.ts`, whose
  directory classifier now also FOLLOWS symlinks/junctions like the engine, from ONE readdir snapshot) ships
  data, never prose, so every topology (desktop, spark, reuse, Python thin client) gets the one authoritative
  reply language. Skipped above the configured `list_dir_hard_cap` (the large-folder rule governs). The
  orchestrator prompt reduces the model's job to a verbatim copy of the `Answer:` line directly under the
  header.
- **The deterministic listing count now reaches BRIDGED setups too** (epic #1144 / #1195): the #1193
  fs-computed `N directories, M files` header was engine-only — but with a client offering local execution
  (`X-Local-Tools: 1`, the standard remote-server + local-code topology) `execute_command` runs in the
  client's tool bridge, which bypassed the engine-side prepend entirely, so live listings started at
  `total …` and the model fell back to self-counting (wrong, self-inconsistent counts, e.g. 21/9 for 20/10).
  The Ink bridge now computes + prepends the same header itself (`listingCount.ts` ≙
  `_listing_count_header_for_command`/`_directory_count_header`: same conservative detection — no header on
  pipes, redirects, globs, `-R`/recursive, >1 path — same filesystem count, only on exit 0 with real output).
  The client-wide symlink parity limit this entry originally carried (a symlink-to-directory counted as a
  file in the client) is CLOSED since #1202: the client's directory classifier follows links like the engine.

### Changed
- **Listings ALWAYS run through the shell — `list_directory` is no longer offered to the model** (epic
  #1144 / #1200): with the tool still offered, the tool choice was sampled per turn, so the transcript look
  flipped between `$ ls -la` output and a `[D]/[F]` list for the same request (operator-verified across
  fresh sessions). The `list_directory` schema is removed from the model's tool list and the orchestrator
  prompt's listing rules are shell-only (large folders: a bounded `ls -lt … | head`, not `sort='time'` +
  `limit`) — determinism lives in code, not in stacked prompt rules. The handler, the client-bridge case and
  `LOCAL_TOOL_NAMES` stay: `/ls` (`manual_ls`) and API callers are unchanged.
- **Listings default to bash `ls -la` with a deterministic count** (epic #1144 / #1193): a shell listing
  (`ls` / `Get-ChildItem`, incl. `cd X && ls -la`) now carries the same exact `N directories, M files` header
  as `list_directory` — computed from the FILESYSTEM (`_directory_count_header`), not by parsing the output —
  so the model copies the number instead of counting the listing (LLMs miscount, e.g. 13/8 for 20/10).
  Detection (`_listing_count_header_for_command`) is conservative: pipes, redirects, globs, `-R`/recursive, or
  more than one path operand get NO header (no guess). The orchestrator defaults a listing to bash `ls -la`
  and copies the header verbatim. (Coloured output follows.)
- **Listings report the TOOL's deterministic count; restored history renders in full** (epic #1144 / #1187):
  a listing result carries an exact `N directories, M files` header and the orchestrator copies THAT number
  verbatim into a one-sentence prose summary naming every item — it never counts the list itself (LLMs
  miscount) and never passes a `limit` that hides items. (The listing path itself has since moved to the
  shell — #1193/#1200 above.) And a resumed session (no `/clear`) renders its
  transcript through the SAME markdown + tool-fold path as fresh output, so restored file names / code are
  coloured, not dim plain text.
- **`list_directory` states a deterministic count** (epic #1144 / #1183): the tool result now leads with an
  exact "N directories, M files" header of the full set (client + engine, byte-parity preserved), so the
  orchestrator reports the count verbatim instead of miscounting the list — LLMs can't reliably count.
  (Since #1200 the tool is no longer offered to the model; it stays live for `/ls` + API callers.)
- **Engine `execute_command` runs the right shell on Windows** (epic #1144 / #1183): a local Windows engine
  hard-forced PowerShell (the tool-bridge fix only helped when a command was bridged to the client). The engine
  now routes per command too — a POSIX/bash command runs in Git Bash when installed, a PowerShell cmdlet in
  PowerShell — and the runtime guidance tells the model both shells work, so it may use either flavour.
- **Git Bash detection resolves `bash` on PATH** (epic #1144 / #1177): the per-command shell routing now also
  finds Git Bash via `where bash` (skipping WSL's `System32\bash.exe`) and Scoop install paths, not just
  Program Files — `GX10_BASH` still overrides.
- **CLI drops the engine's transient tool-generation status** (epic #1144 / #1181): the engine's transient
  "generating tool call" status line (`⋯ …`) leaked into and lingered in the committed transcript. The client
  has its own working-line spinner, so the output router now drops any `⋯`-prefixed engine status line.
- **Orchestrator answers simple results in prose, not a table** (epic #1144): a set of files / names / options
  is now concise prose (e.g. "contains 19 directories (…) and 10 files (…)") or a short bulleted list, instead
  of a wide single-cell table that overflowed the terminal — the operator never asked for a table (prompt
  guidance). Reserve tables for genuinely multi-column data.
- **CLI runs the right shell per command** (epic #1144 / #1177): the local tool-bridge hard-forced PowerShell
  on Windows, so a bash command (`ls -la`) couldn't run and was mislabelled `Bash(...)`. It now routes per
  command — PowerShell cmdlets (`Get-ChildItem`, `Select-String`, `$env:`) run in PowerShell, POSIX/agnostic
  commands in Git Bash when installed (`GX10_BASH` overrides the detection) — so BOTH shells work, neither is
  forced, and the tool-call header names the shell each command actually ran in.
- **CLI text selection follows the scroll + drags past the edge** (epic #1144 / #1173): the selection is now
  content-coordinate — scrolling (wheel / PageUp / PageDown) keeps the highlight on the selected text instead of
  stranding it on fixed screen rows, and dragging past the top/bottom edge auto-scrolls so a selection can span
  more than one screen; copy reads the FULL content, not just the viewport. Replaces the earlier
  clear-on-scroll workaround.
- **CLI tool calls are foldable, Claude-Code style** (epic #1144 / #1167): a tool call renders collapsed as an
  action summary (`● Ran 1 shell command` / `● Read 70 lines`, present-progressive `Running…`/`Reading…` while
  it runs) with a one-line detail (`⎿ $ <cmd>` / `⎿ <path>`); click it to expand the exact `Bash(<cmd>)` /
  `Read(<path>)` header + the full result under a `⎿` corner, click again to collapse. The result is folded,
  never truncated; fold state persists across re-renders. The expanded header names the client's ACTUAL shell
  (`PowerShell(<cmd>)` on Windows), not a hardcoded `Bash`, since the local tool-bridge runs the platform shell.
  A blank line inside a tool result (e.g. PowerShell output, whose first line is blank) no longer ends the
  block early and leaks the rest into the transcript as raw, coloured text — the whole result stays folded.
- **CLI polish from live testing** (epic #1144 follow-ups): the tool-call header is now muted grey (not
  magenta/pink); the orchestrator prompt forbids indenting markdown (an indented block was rendered by the CLI
  as a raw code block — literal `#`/`|`/`>` — instead of formatted markdown); and a text selection is dropped
  on scroll instead of leaving the highlight stranded on fixed screen rows (selection is screen-coordinate).
- **CLI tool calls read like Claude Code** (epic #1144 / #1146 + #1147): a tool call now shows a human header —
  the command / target (`● Bash(ls -1)`, `● Read(path)`, `● Issue(title)`), NOT the internal
  `execute_command(command='…')` chrome — and its FULL result indented under a `⎿` corner, instead of a
  single-line 70-char preview that cut mid-word. Overlong output is capped with an explicit
  `… (+N more lines)` marker — never a silent truncation.
- **CLI input box is pinned to the viewport bottom** (epic #1144 / #1148): the input + footer no longer scroll
  off when you scroll up through history. The renderer now paints the transcript scrolled and then STAMPS the
  fixed chrome (working/thinking line + input + menu + footer) at the bottom rows (new `paintFixed` +
  `Surface.clearRows`), so it stays
  put while history scrolls behind it — the Claude-Code behaviour, on the same alt-screen + app-ScrollBox tech.
  Plus a blank line above the input's top rule.
- **CLI tables + emphasis now render (root-cause fix)** (epic #1144 / #1154): the engine's `_TableLineRenderer`
  was collapsing every pipe table into pipe-less aligned columns and stripping `**` before the stream reached
  the client — a pre-markdown-client leftover that made the Ink client (which renders markdown itself) show
  tables as flat text and bold as plain text. It now re-emits pipe tables as PROPER GFM (pipes kept + the
  `|---|` separator inserted) and passes bold/code through unchanged, so the client renders a box and shows
  emphasis. This is why the operator's tables were "wordsalat" — the model's markdown never reached the client.
- **CLI tolerates separator-less pipe tables** (epic #1144 / #1152): a smaller model often emits a
  markdown table without the `|---|` separator row, which GFM would render as flat pipe-text; the renderer now
  repairs it (inserts the separator) so it still renders as a box. Well-formed tables and non-table pipe text
  are untouched. (The table renderer itself was verified correct via the render-to-buffer harness — the
  original breakage was model fidelity, not rendering.)
- **CLI markdown formatting parity with Claude Code** (epic #1144 / #1150): beyond the colour muting — lists
  use `- ` bullets at a tight indent (top level at column 0, nested 2-space) instead of `* ` + deep indent; a
  blockquote renders as a `▎ ` left bar on its **own** line (no longer glued onto a preceding list/paragraph
  line); inline code and links are **indigo** (`#818cf8`), not grey. Verified against a live
  Claude-Code reference render.
- **Muted, Claude-Code-like CLI palette** (epic #1144 / #1145): the Ink client's markdown rendering drops
  `marked-terminal`'s colourful default (green headings, yellow inline code, blue-underlined links, red table
  headers) for restrained emphasis — **bold** headings (no literal `##` prefix), **dim** code, quiet links,
  uncoloured tables. Less colour, same structure. Raw-ANSI style functions, no new dependency.
- **`create_issue` is now capability-detected (default ON)** (epic #1043 / #1073): it was the only optional
  tool behind a manual opt-in flag (`forge.enabled`, default off) while every other optional tool
  (`web_search`, memory, skills, prompts) is capability-detected. It is now offered whenever the `gh` CLI is
  present + authenticated — installing/authing `gh` IS the operator's deliberate opt-in — via a new
  `_forge_available()` that mirrors `_web_search_available()`. The operator can still force it off
  (`forge.enabled=false` / `GX10_FORGE_ENABLED=0`), and it is blocked under the `sealed` trust profile (no
  autonomous outbound writes, like `web_search`/`fetch_url`). The tool surface is now uniformly
  capability-detected — no redundant flag to flip.
- **`create_issue` hardening — valid labels + native sub-issue linking** (epic #1043 / #1130 follow-up): (1)
  the `labels` arg is now validated against the repo's ACTUAL labels — an unknown label is rejected with the
  valid set + a did-you-mean so the model re-emits (validate→reask), instead of `gh` hard-failing the whole
  create on the first invented label (and instead of silently dropping it); fail-soft (a label-lookup hiccup
  skips validation, never blocks a create). (2) A new optional `parent` param links the new issue as a native
  GitHub **sub-issue** of an epic via `gh issue edit --parent`, so the model links in-tool instead of via
  ad-hoc `execute_command` gh calls; fail-soft (a link failure is reported alongside the created issue, not
  raised).

## [0.0.25]
### Added
- **Query-aware rolling summary (L3)** (epic #1043 / #1049): on context eviction the hierarchical rolling
  summary now biases retention toward the current user task — the turn is captured at `run()` entry and
  folded into the summarizer instruction as a *bias, not a filter* (recency eviction and relevance recall
  via RAG are unchanged; this only steers what the summary keeps when space is tight). Fail-soft: with no
  turn in scope the generic instruction is used, byte-identical to before, and the injected focus is bounded
  so a large paste can't bloat the summarizer's own prompt. No new flag. First code rung of the
  context-&-quality defense-in-depth epic (#1043).
- **In-core secret-env hardener on the CLI-runner lane** (epic #1043 / #1052): `default_cli_runner` spawned
  the coder subprocess (`web_search`, `parallel_reason`, and the future `read_offload`) with the server's
  full environment. A new `engine/agent_env.py` now scrubs every secret NAME from the child env and redirects
  the git/gh credential-discovery paths into an empty scratch store at that one choke-point, so a
  prompt-injection in untrusted ingested content can't exfiltrate an inherited token or push via the ambient
  credential — while `HOME`/`~/.claude` (the coder's own OAuth) is preserved and `CLAUDE_CONFIG_DIR` is never
  set (#994/#996). Fail-closed to a plain secret scrub if the redirect can't be written. Also closes the
  pre-existing `web_search` env exposure; a hard precondition for the gated `read_offload` (#1053).
- **Emergency-rung archive + optional summarize-not-truncate (L3)** (epic #1043 / #1050): the last-resort
  recovery that truncates the largest turns' content to head+tail excerpts previously dropped the middle
  slice silently. It now **always cold-archives** the discarded slice (`add_bulk(source="fragment_trim")`)
  so B2 RAG can re-inject it query-aware next turn. An optional, **default-off** `context.emergency_summarize`
  (`GX10_EMERGENCY_SUMMARIZE`) replaces the raw drop with a bounded summary, wrapped in a hard wall-clock
  timeout (daemon-thread, win32-safe), skipped when a generation this turn already errored, and **always**
  falling through to raw truncation on timeout/exception (at most one model call per invocation). The
  default-off path is byte-identical to before.
- **Proactive ingestion accountant + shared summarize rate-limit (L3)** (epic #1043 / #1051): completes the
  L3 backstop. A **default-off** `context.proactive_roll` (`GX10_PROACTIVE_ROLL`) accountant runs at the
  tool-result boundary and, once cumulative ingestion crosses `context.ingest_soft_frac` (~0.7) of the model
  window, proactively sheds the oldest whole tool rounds via a query-aware roll-summary (high floor) instead
  of waiting for the reactive low-floor truncation. A `context.max_summaries_per_turn`
  (`GX10_MAX_SUMMARIES_PER_TURN`, **0 = unlimited**) shared per-turn cap now bounds the total summarizes
  across ALL three triggers (steady-state roll, emergency rung, proactive) so they can't compound into
  multiple full model round-trips in one turn; past the cap a roll degrades to a plain archived drop.
  Per-turn counters (summaries + estimated tokens) track the cost. The default path is byte-identical.
- **Ranged / pattern `read_file` (L1)** (epic #1043 / #1047): `read_file` gains `start`/`end` (1-based
  inclusive line numbers), an optional regex `pattern` (reads a window of lines around the first match), and
  `max_chars` — so the model reads only the relevant slice of a large file instead of the whole thing, and
  the schema/description steer search-first (use `search_files` to locate the lines, then read that range). A
  bad range or an unmatched/invalid pattern falls back to the existing head+tail cap (never crashes). The
  slice logic is mirrored in the TypeScript client (`clients/ink` `runTool.ts`) so a local-topology read
  applies the same slice, and the ink omission marker is re-steered from `findstr`/`Select-String` to
  `search_files` (matching the server, #1046).
- **L0 served-window deploy passthrough (64k)** (epic #1043 / #1044 / #1045): `scripts/spark-bootstrap.sh`
  gains `--max-model-len` / `--max-num-seqs` flags (previously env-only) and logs the effective window before
  its idempotency check, and the private deploy driver now forwards `IRONCLAD_MAX_MODEL_LEN` /
  `IRONCLAD_GPU_MEM_UTIL` / `IRONCLAD_MAX_SEQS` to the remote (ssh does not forward env), so a deploy raises the
  served vLLM window to 64k reproducibly without hand-editing the bootstrap default. The public core default
  stays a conservative 32768 and the engine auto-adopts the served window at boot (#377). Live-verified on the
  Spark: vLLM serves 65536 with the co-located Mem0 stack healthy (~43x KV headroom at 65,536 tokens/request,
  ~37 GB free) at gpu-mem-util **0.6** — the evidence corrects the design's 0.85, which would starve Mem0 on
  the shared 121 GB unified-memory box; unset `IRONCLAD_MAX_MODEL_LEN` reverts to 32768 cleanly.

## [0.0.24]

### Added
- **Per-principal identity + RBAC + multi-tenant memory namespacing (foundation)** (epic #1065 / #1071):
  Ironclad's trust model was single-tenant (one token for the whole server, no per-principal identity/authz,
  no tenant isolation). A pure core authz foundation (`ack.authz`): a `Principal` (id/role/tenant), a
  deny-by-default `DEFAULT_ROLES` RBAC policy (role → allowed danger-tiers; admin=all, operator/agent=
  all-but-destructive, reader=read_only), `authorize(role, tier)`, `resolve_principal(token, principals)`
  (secret-free — token values from env), and `tenant_scope(scope, tenant)` for isolated multi-tenant memory
  (default tenant = no-op). Gated `security.multi_tenant` / `GX10_MULTI_TENANT` (default off, byte-identical);
  engine seam `_authorize_action` / `_tenant_mem_scope`. **Foundation only**
  ([ADR-0014](docs/adr/0014-rbac-multi-tenant.md)) — full request-path authz, ABAC, memory-service tenant
  enforcement, and per-tenant routing are explicit remaining scope (overlaps the Enterprise milestone #20).
  **Completes the Autonomy-safety epic (#1065).**
- **OS-level execution sandbox for agent-run commands** (epic #1065 / #1069): `execute_command` ran agent-
  generated shell with the orchestrator's own privileges. A core sandbox seam (`engine/sandbox.py`) now wraps
  the command in an unprivileged OS isolation backend (bubblewrap / firejail) when one is configured + on PATH
  — the foundational win is **network isolation** (`--net=none` / `--unshare-net`: no exfiltration / no C2)
  while the filesystem stays accessible so legit commands work. `available_backend` detects the tool;
  `wrap_command` builds the wrapper (pure); wired into `execute_command`'s POSIX branch via `security.sandbox`
  / `GX10_SANDBOX` = off | auto | bwrap | firejail (default off). The seam never claims containment it can't
  provide (no backend → runs as-is). Defense-in-depth ([ADR-0013](docs/adr/0013-exec-sandbox.md)) — full FS
  isolation, seccomp, a container-runtime backend, and Windows containment are explicit remaining scope.
- **Prompt-injection defense on the ingestion paths** (epic #1065 / #1068): ingested content (files, search/
  web results, directory listings, tool output) had **no trust boundary** — an autonomous agent reading it
  could be steered by an embedded instruction-override / role-switch / tool-injection. A layered core defense
  (`ack.injection`): a precision-first `scan` for injection patterns + a `wrap_untrusted` trust boundary that
  fences every ingested result as *data, not instructions* at the #1046 ingestion choke point, with a warning
  when injection is detected. Gated `security.injection_defense` / `GX10_INJECTION_DEFENSE` (default off,
  byte-identical). Defense-in-depth ([ADR-0012](docs/adr/0012-ingestion-injection-defense.md)) — **not** a
  complete solution; an LLM classifier, per-source trust levels, and output-side checks are explicit
  remaining scope.
- **Learned-state safety — eval-gated ACE promotion + snapshot + auto-revert** (epic #1065 / #1070): ACE
  deltas were applied without a snapshot or an eval gate, so a bad learned delta could silently degrade
  behavior. With `ace.safe_promote` on, `PlaybookStore.adapt` now **snapshots the pre-adapt playbook** (an
  operator/auto rollback point, reusing the #1082 history) before every online adaptation, and — when an eval
  scorer is wired (`set_transports(eval_fn=…)`, a held-out eval / telemetry-derived signal, deploy-provided)
  — **auto-reverts a measured regression** (`ack.ace.robust.regression_verdict`: never persists a delta that
  lowered the score; fail-open on a broken measurement). Default-off (byte-identical); a built-in live-eval
  signal is explicit remaining scope. Builds on #1082's snapshot/rollback/unlearn.
- **Ambiguity auto-detector — the no-guessing rule becomes a pre-flight gate** (epic #1065 / #1066): the
  no-guessing rule was a prompt CONVENTION (Variant A: the agent had to NOTICE an ambiguity and declare a
  `ForkSignal`). Variant B is the autonomous safety net — `ack.ace.fork.detect_ambiguity` is a pure,
  precision-first pre-flight scan that flags requirement underspecification (uncertainty markers, an open
  question inside the requirement, either/or, vague qualifiers, hedges) and emits the same halt-to-ask
  `ForkSignal`. Wired (**default-off** `safety.ambiguity_detect` / `GX10_AMBIGUITY_DETECT`) into the
  `pre_handover` Hook-Bus, so an autonomous agent that did NOT notice the ambiguity is warned with the fork
  question + options instead of guessing. **Observer-only** (warns; never gates the fail-closed path — full
  pipeline-HALT enforcement is operator policy, explicit remaining scope); byte-identical when off. First
  sub-issue of the Autonomy-safety epic.
- **Alerting pipeline — page the SLO/anomaly + receive external alerts** (epic #1059 / #1061): a degradation
  no longer only prints to a console. `engine/alerting.py` turns the telemetry SLO/anomaly verdict (#1060)
  into alerts (SLO breach → critical, anomaly → warning), each correlated with the running deploy version;
  the engine **outbound**-pages them to the configured webhook (#1083). A gated periodic self-scan
  (`alert.enabled` + a webhook → a server daemon every `alert.interval_s`) fires them automatically, and a
  token-gated **inbound** `POST /alert` receiver pages an external monitor's alert. Default-off (no scanner
  thread when off, byte-identical); stdlib-only; fail-soft. **Completes the Operate & Maintain epic (#1059).**
- **Operate-phase scheduler primitive** (epic #1059 / #1064): the orchestrator's `execute_command` is
  single-shot (and refuses `schtasks`/`start-job`), so periodic operate jobs (backup, prune, drift checks)
  had no in-product scheduler. `scripts/scheduler.py` fills the gap — a jobs config (`{name, command,
  interval_s}`), a last-run state file, and a `--run-due` pass that runs whatever is due (a failed job retried
  only after its interval, never in a hot loop). Driven by ONE host cron entry that fans out to every job;
  `scripts/scheduler.jobs.example.json` seeds backup (daily) + prune-runs (weekly); `docs/scheduler.md` is
  the runbook. Third sub-issue of the Operate & Maintain epic.
- **Backup & restore for the memory tiers + engine state** (epic #1059 / #1062): the crown-jewel accumulated
  memory (Qdrant vectors, Neo4j graph, Valkey warm) + engine state (vault artifacts, ACE playbooks, the audit
  ledger) had **zero** backup automation — data loss was permanent. `scripts/backup.sh` dumps each running
  memory-tier volume (via a throwaway helper container, so it works without quiescing the DB) + the
  `./ironclad-workdir` bind mount to a timestamped archive, then applies retention (keep newest N,
  unit-tested); `scripts/restore.sh` puts it back (`--yes`-gated, DESTRUCTIVE); `docs/backup-restore.md` is
  the runbook (incl. cron scheduling until the in-product scheduler #1064). Second sub-issue of the Operate &
  Maintain epic.
- **Runtime self-telemetry — `GET /metrics` + SLO/anomaly** (epic #1059 / #1060): the orchestrator keeps a
  bounded, thread-safe rolling record of per-generation latency, token cost, and errors (`engine/
  telemetry.py`), and a token-gated `GET /metrics` returns the all-time + recent-window aggregate (turns,
  error rate, latency p50/p95, prompt/completion/total tokens) plus an **SLO verdict** (config-tunable
  `metrics.slo_error_rate` / `slo_p95_latency_s` / `window_s`) and a **recent-vs-baseline anomaly** signal —
  so a degradation in an unattended deployment is observable/scrapable without a human at the console.
  stdlib-only, fail-soft (recording never breaks a turn). First sub-issue of the Operate & Maintain epic.
- **Per-action audit ledger** (epic #1043 / #1084): the orchestrator's mutating tool actions (`write_file` /
  `write_last_reply` / `edit_file` / `execute_command` / `create_issue`) can now be recorded into a
  **tamper-evident, hash-chained** audit trail (`engine/audit_ledger.py` — a core-owned ledger reusing the
  dev-process ledger's proven `sha256(seq|prev_hash|payload)` chain; `verify_chain` detects any edit / reorder
  / middle-deletion). Records are **content-free** (actor + action + target + ok, never the file body or
  command output) and **default-off** (opt-in `audit.enabled` / `GX10_AUDIT_ENABLED` — no records when off,
  byte-identical). The minimal first step of the audit-log epic (#1067); fail-soft. The last capability-audit
  quick-win — completes epic #1072.
- **Escalation notifications — a HUMAN_ESCALATION reaches an off-duty human** (epic #1043 / #1083): a
  HUMAN_ESCALATION (the per-task attempt budget spent) now fires a new `escalation` Hook-Bus event, and a
  default-off notifier POSTs it to an operator-configured webhook (a Slack incoming webhook or any JSON
  endpoint) so it no longer only prints to a console a human must watch. The endpoint is a **deploy secret**
  (`GX10_NOTIFY_WEBHOOK` / `notify.webhook` — never a URL literal in core); with no URL configured no consumer
  is registered (byte-identical). stdlib-only, fail-soft; seeds the Operate epic's alerting. The tenth
  capability-audit quick-win.
- **`/ace snapshot|versions|rollback|unlearn` — operator safety net for the learned playbook** (epic #1043 /
  #1082): the ACE playbook adapts silently, but the M-002 versioning + Q-001 selective-forget primitives
  (`ack.ace.robust`) were unreachable. The `PlaybookStore` now persists a bounded per-scope version history
  (next to the scope playbook) and exposes `snapshot` (record a rollback point), `versions`, `rollback
  [<version>]` (restore the previous or a named snapshot — snapshotting the current state first so a rollback
  is itself reversible), and `unlearn <id…>` (selectively forget bullets); wired to the `/ace` verbs,
  fail-soft. The ninth capability-audit quick-win.
- **Log & run-artifact retention** (epic #1043 / #1079): the reference compose stack now caps every
  long-lived service's json-file logs (`max-size: 10m` × `max-file: 3`, via a shared `x-logging` anchor) so an
  always-on deployment can't fill the disk with unbounded container logs; and a new `scripts/prune_runs.py`
  purges run artifacts older than `--keep-days` (default 30) from the workdir's `runs/` directories (dry-run
  by default, `--apply` to delete) — a deploy schedules it (cron/timer today; the in-product scheduler is
  #1064). The seventh capability-audit quick-win.
- **Supply-chain: dependabot + a CI dependency-audit** (epic #1043 / #1078): **dependabot runs on the source
  repo** (bumping `core/pyproject.toml` / `clients/ink` / the workflows, so a fix flows through core/ → the
  export — never a doomed edit of the generated public tree), and the published repo's `dependency-audit` CI
  job runs `pip-audit` (Python) + `npm audit` (ink) to surface known CVEs on the shipped artifact. **Advisory**
  (continue-on-error, not a required check) so a newly-disclosed transitive CVE surfaces in the log without
  blocking unrelated PRs. The sixth capability-audit quick-win.
- **`feature-spec` / PRD authoring prompt** (epic #1043 / #1077): a new `kind: prompt` built-in in the core
  prompt library that drafts a concise product feature spec / PRD (problem · users & use cases · goals /
  non-goals · prioritised MUST/SHOULD/COULD requirements · acceptance criteria · risks & open questions) from
  a `feature` description — closing the ideation/planning gap of a product-facing artifact. EN + DE; passes
  `ack.gate`; no engine change (one MD file). The fifth capability-audit quick-win.
- **`remember` — deliberate durable memory write** (epic #1043 / #1076): a `remember(text)` tool lets the
  model deliberately persist a fact / decision / gotcha into the project's long-term memory (fire-and-forget
  via `MemoryManager.add_bulk`, scope-aware to the active project) so it survives the session and is retrieved
  later via `query_memory` / RAG — the write counterpart to the read-only `query_memory` / `deep_query_memory`.
  Offered only when a memory store is configured; fail-soft. The fourth capability-audit quick-win.
- **`edit_file` — targeted string edit** (epic #1043 / #1075): `edit_file(path, old_string, new_string,
  replace_all?)` replaces an EXACT string in a file instead of rewriting the whole thing with `write_file`
  (cheaper + safer for a small change). `old_string` must match exactly and be unique (add surrounding
  context) unless `replace_all`; an absent or ambiguous match is refused (file unchanged); atomic write with
  the retry-on-lock helper. The third capability-audit quick-win.
- **`fetch_url` — read a specific web page verbatim** (epic #1043 / #1074): a bounded `fetch_url(url)` tool
  fetches an http(s) page (RFCs, standards, API specs, docs) via stdlib `urllib` — `web_search` FINDS pages,
  `fetch_url` READS one. Trust-gated (offered only when outbound is allowed; blocked under `sealed` unless
  `security.web_in_sealed`), **SSRF-guarded** (http/https only; refuses loopback/private/link-local hosts so
  an autonomous agent can't pivot to internal services, incl. cloud metadata `169.254.169.254`), and
  double-bounded (a hard byte cap + the ingestion choke-point char cap). The second capability-audit
  quick-win.
- **The orchestrator can file its own tracker issues — gated `create_issue` tool** (epic #1043 / #1073): a
  new model-callable `create_issue(title, body_file, labels?, milestone?)` files an issue in the project's
  code forge (GitHub, via the `gh` CLI) — so the orchestrator no longer dead-ends at "I have no GitHub tool"
  and writes an un-submittable body file. **Default OFF** (opt-in `forge.enabled` / `GX10_FORGE_ENABLED` — an
  autonomous agent writing to a forge is a deliberate operator choice; the tool is not registered when off,
  byte-identical), **secret-free** (ambient `gh` auth, no token on the wire, no repo literal in core; optional
  `forge.repo`), and **escape-free** (the body comes from a FILE the model already wrote via `write_last_reply`,
  never a giant JSON arg). The first quick-win from the whole-lifecycle capability audit.
- **Escape-free authoring — `write_last_reply` + `write_file` append** (epic #1043 / #1048, L1): a small
  model can't reliably emit a large file body as a JSON-escaped `write_file` `content` string (it mis-escapes
  → the tool JSON is malformed → the write is dropped). Two escape-free paths now exist: a new model-callable
  **`write_last_reply(path)`** persists the model's PREVIOUS reply text (produced as ordinary streamed output
  — no JSON escaping), and **`write_file` gains `mode='append'`** to build a large file in chunks. A warn-only
  integrity guard flags a write whose emitting generation was cut off by the token limit
  (`finish_reason=length`, now captured in the completion metrics) — the file may be truncated, continue with
  append. Never blocks.

### Changed
- **Full-surface, immutable per-action audit log — who/what/when/why + tamper-resistance** (epic #1065 /
  #1067): extends #1084 from the mutating subset to the FULL tool surface. `audit.scope: all` (vs the default
  `mutating`) records EVERY tool call (reads, searches, memory queries, …); each record now carries
  who/what/when/why (actor + action + content-free target + `reason` = the active project scope + ts). And
  the agent's own write tools (`write_file`/`edit_file`) REFUSE any path under the audit directory
  (`_is_audit_path`), so an autonomous agent can't tamper with its own trail — tamper-RESISTANCE on top of the
  hash-chain's tamper-EVIDENCE. Default-off; append-only OS enforcement / an external WORM sink is explicit
  remaining scope.

### Fixed
- **One ingestion choke-point cap — no single tool result overflows the window** (epic #1043 / #1046, L1):
  every INGESTION tool result (`read_file`, `list_directory`, `search_files`, `execute_command`) is now
  capped to the LIVE per-turn budget at ONE run-loop choke point — not just `read_file` (which capped
  itself) but the previously-uncapped `search_files` / `list_directory` / `execute_command` AND the
  local-tool bridge return (which bypassed read_file's cap). Structured / already-budgeted payloads
  (`web_search`, `parallel_reason`, MPR, memory) are never touched. `read_file`'s omission marker now steers
  to `search_files` (a capped tool) instead of the uncapped `execute_command`/`findstr`. So an agentic turn
  that fans out many large reads stays bounded per result — the first of the epic #1043 context/quality
  quick-wins.
- **A malformed tool-call no longer kills the turn — Validate→Reask self-heals** (epic #366): the
  `arguments` stored on an assistant `tool_call` in the history are now sanitised to valid JSON
  (`_valid_tool_args_json`) before they enter the transcript. vLLM `json.loads()` the stored tool-call
  arguments when rendering the NEXT request, so a model's un-parseable arguments string (a small model
  emitting a huge escaped `content` for `write_file` gets it wrong) previously hard-400'd the reask itself
  (`Expecting ',' delimiter`) and defeated the tool-boundary Validate→Reask — the turn died before the
  model could re-emit. The parse error is still fed back as the tool result (the model re-emits), but the
  request always renders, so the loop actually recovers. **Defense-in-depth:** the sanitiser also runs over
  the WHOLE history at the single send choke-point (`_sanitize_tool_call_history` in `_make_completion`), so
  a malformed tool-call that entered from a **loaded session** (`session.json` persists the raw arguments)
  or a resume can't 400 the FIRST request after a restart — the operator hit exactly this: a truncated
  `write_file` call reloaded from a prior session produced a 0-generation 400 on the next run.
- **Adaptive output reserve — a marginal-overflow turn no longer dies** (epic #366): the pre-flight context
  guard now treats the output-token reserve (`generation.max_tokens`, default 8192) as a **ceiling, not a
  fixed floor**. When the full reserve would push the prompt past the model window, the engine reserves
  **less** output — down to `context.min_output_tokens` / `GX10_MIN_OUTPUT_TOKENS` (default 1024) — so the
  turn proceeds **losslessly** (all context kept, just a shorter answer) instead of raising a
  `ContextOverflowError`. Only when even a minimal answer will not fit does it emergency whole-round trim,
  then truncate an irreducible oversized turn (#994-S16), then — last — raise. Fixes routine multi-file
  turns overflowing a 32k window by a handful of tokens (a large prompt + the fixed 8192 reserve landing
  just over the wall) on a 32k-window model such as `qwen3.6-35b`. The clamp keeps a
  `context.overflow_safety_tokens` / `GX10_OVERFLOW_SAFETY` headroom (default 1536) below the window so it
  never targets the wall to the token — the engine's estimate undercounts vLLM's exact rendered prompt
  (chat-template framing + tools/tool-call serialization), and a zero-margin send would still hit a raw
  vLLM 400. And when whole-round eviction cannot fit the transcript, the recovery now **iteratively**
  truncates the largest turns' content (not a single message): an agentic loop is ONE user turn with many
  accumulated tool reads and no round boundary to evict, so a single truncation left it over the wall — the
  loop now degrades to head+tail excerpts and keeps running instead of raising.

## [0.0.23]
- **Budget-aware read cap + emergency-trim recovery** (epic #994 / #1028): an agent can no longer overflow
  the model window with a single tool result. `read_file` now caps to the live per-turn window budget
  (window − reserve − transcript, ×0.8, floored) instead of a fixed ceiling — so one read can't overflow on
  any model — falling soft to the fixed cap without an exact tokenizer; and the pre-flight guard's emergency
  trim now TRUNCATES an irreducible single oversized turn (with a marker) instead of raising, so the turn
  degrades gracefully rather than failing.
- **Coder Memory MCP is always-on** (epic #994 / #1015): the read-only Memory MCP for the code agents is
  no longer gated on the `sealed` trust profile — `memory_mcp.render_mcp_launch` wires it whenever a
  memory service (`memory_url`) is configured AND the agent ships a per-CLI `mcp_template`, in ANY
  profile. Safe: the MCP exposes only read tools (`memory_search` + `memory_deep_query`, no write), so a
  coder can only READ project memory. All four roster coders (OPUS/SONNET/CODEX/KIMI) now ship a template
  (KIMI's was added). Inert until a memory service is configured (byte-identical launch otherwise).

### Added
- **The remaining command-ergonomics chrome is localized + `/config keys` shows values** (epic #927 / #956,
  completing the audit follow-up): the `/config keys` header, the danger-tier `/help` block, the config-set
  unknown-root refusal, and the `/skills` params label now route through `_msg`/`messages.py` (EN source + DE
  overlay, no hardcoded German); `/config keys` renders each key's **current value + inferred type** (not just
  the name); and the destructive-confirm message is a **single localized line** (reason + how-to-confirm from
  the server, printed verbatim by every client — no English wrapper mixing into a translated reason). The
  shipped **destructive-only** confirm scope is reconciled across the epic AC + docs (costly keeps its own
  guards; boot-only via the frozen-key refusal).
- **Every client renders the guided-input contract** (epic #927 / #955): the ink client (field-by-field,
  with choice pick-lists + defaults + the canonical echo) and the three Python REPLs (via a shared
  `client.render_guide`) now detect the server's `needs_guide` reply on `/chat/stream` exactly as they
  detect `needs_confirm`, so `/<verb> ?` or `/<verb> --guide` shows the fields the operator must supply
  instead of a blank or a billed turn. Client chrome stays English (thin renderer).
- **Server-side structured guided-input contract** (epic #927 / #954): on an explicit `/<verb> ?` or
  `/<verb> --guide`, the server (`_guide_required`, sibling to the confirm gate) replies
  `{needs_guide: {command, subcommands, fields:[{name, required, choices, default, type}], usage,
  canonical_echo}}` — the fields the operator must supply, derived from the command-spec — instead of
  executing. Uniform across every client; the explicit affordance only (never auto-launch on a bare or
  partial command, C0 #6); served on `/chat` + `/chat/stream`; fail-soft; `_dispatch` untouched (it is a
  pre-execution info reply, the spec describes the executor and never drives it).
- **Every flag-heavy worst-offender is now guided from the command-spec, not a hand-written string**
  (epic #927 / #953): `/config set`, `/ace`, `/project`, and `/generate` emit `command_spec.guided_usage(verb)`
  when under-specified (previously each hand-wrote its own usage line, free to drift from the spec + the
  `/catalogue` hint). A `usage` override on the spec carries the one multi-form verb (`/project`'s
  `new <name>` / `delete <id>` forms) that a flat subcommands+flags render cannot; `render_usage`
  (the `/catalogue` hint) and `guided_usage` (the dispatch line) share that single source.
- **The terminal client's static command mirror is now complete + coverage-guarded** (epic #927 / #952):
  the ink `COMMANDS` server subset was missing `lifecycle`/`fork`/`ace`, which silently blinded the
  did-you-mean net to exactly those worst-offender verbs (a typo like `/lifecyle` was forwarded and billed a
  model turn instead of being suggested). The three verbs are added, `check_ink_command_parity.py` now
  asserts the ink server subset **covers** `command_spec.verbs()` (fail-closed — completing #940's
  spec↔generated-client binding), and argument autocomplete now prefers the richer `/catalogue` entry over
  the static fallback so a completed verb's flags/choices still resolve.
- **Two fail-closed drift guards for the terminal client** (epic #927 / #939, completing the epic):
  `check_ink_command_parity.py` asserts the ink client's static `ALIASES`/`UNSAFE` mirror equals
  `engine.command_spec.ALIASES` / `unsafe_first_words()` (the single source — the ink cold-start fallback
  can no longer silently drift), wired into private CI alongside the command_spec↔dispatch guard (#940).
  `check_ink_test_count.mjs` runs the `node:test` suite and asserts the documented TypeScript counts
  (README + status + test-report) match it — the TS analogue of `gen_test_counts.py` — wired into
  node-client.yml.
- **Command-ergonomics documentation + engine-chrome i18n** (epic #927 / #938): a public
  [`docs/command-ergonomics.md`](docs/command-ergonomics.md) documents the whole surface — aliases,
  did-you-mean, unambiguous prefix, argument autocomplete, discovery, confirm-before-destructive, and the
  `/ace` ergonomics. The new user-facing **engine** outputs (the destructive-confirm reason and the `/ace`
  verdict) are localized through the message catalog (`engine/messages.py`, `_msg`) — English is the source
  and default, German is an overlay, and no German is hardcoded in core.
- **Argument autocomplete in the terminal client** (`clients/ink`, epic #927 / #937): once past the verb,
  the slash-menu completes a command's **subcommands, flag names, and flag choices** from the structured
  `flags`/`subcommands` the server now serves in `GET /catalogue` (#936) — e.g. `/lifecycle ` → `gate`,
  `/lifecycle gate --` → `--slug --tree --ledger --stages`, `--stages ` → `tests|reviews|delivery`.
  Deterministic + zero-cost (no model); accepting inserts the token into the line (it does not reset it to
  `/verb`), and the already-server-driven did-you-mean (#934) + confirm (#935) surfaces render in the same
  client. The static command list stays the cold-start fallback.
- **Spec-derived command guidance + `/ace` ergonomics** (epic #927 / #936): the command-spec now renders a
  single-source usage line (`command_spec.guided_usage(verb)` — subcommands + flags, required bare /
  optional `[bracketed]` / choices `{a|b}`), wired into `/lifecycle`'s usage so it can never drift from
  the spec; and `GET /catalogue` entries carry structured `subcommands` + `flags`
  (`{name, required, choices, summary}`) so a client can build argument autocomplete + guided input from
  the one source. `/ace warmup|eval` now **defaults its `--ledger`** to `<root>/.devloop/ledger.jsonl`
  (like `/lifecycle gate`, no required flag to type), and `/ace eval` reports a **plain-language verdict**
  ("ACE learned from N run(s) using X model call(s) … that is Y% fewer than the evolutionary baseline")
  with the paper's J-001/J-002 kept only as a parenthetical.
- **Server-side confirm-before-execute for destructive commands** (`engine.server._confirm_required` +
  the `/chat` + `/chat/stream` gate + the clients' `--yes` affordance, epic #927 / #935): a destructive op
  (currently `/project delete`, incl. `--purge`) is no longer run on first ask — the server (the single,
  uniform authority; danger-tier from the command-spec, never model-graded) replies with a
  `{needs_confirm: {command, tier, reason}}` instead of executing. Every client (ink + the Python
  line/rich/TUI REPLs) shows the warning and re-runs with a trailing `--yes` to confirm (stripped +
  sent as `confirm: true`). Read-only/mutating/costly commands are unaffected (operator-scoped to
  destructive only); `_dispatch` untouched; fail-soft (a gate hiccup never blocks a normal turn).
- **Alias / unambiguous-prefix / did-you-mean in the command router — a mistyped `/verb` no longer costs a
  model turn** (`engine.command_spec.resolve_command` + `engine.commands.classify` + `clients/ink`
  `classify`, epic #927 / #934): short aliases (`/lg`→`lifecycle gate`, `/cfg`→`config`, …), an unambiguous
  **non-destructive** prefix (auto-resolved; a destructive/costly verb only *suggests*), and a did-you-mean
  for a close typo — all **deterministic + zero-cost** (no model), resolving to the exact canonical command
  re-parsed through the untouched `_dispatch`. A typo becomes a suggestion (`kind='suggest'`, shown by every
  client, never forwarded) instead of an unknown `/verb` that reached the server and billed a turn; a
  `/<prompt-name>` still forwards so the server's prompt resolver runs. The alias table + the destructive/
  costly "unsafe" set are the command-spec's (single source; the ink mirror is parity-guarded).
- **`/lifecycle gate` resolves a default delivery tree from git HEAD when `--tree` is omitted**
  (`engine.gx10._git_head_tree`, epic #927 / #933): the worst-offender's un-typable `--tree <sha>` now
  defaults to the committed HEAD tree (`git rev-parse HEAD^{tree}`) so an operator can run `/lifecycle gate`
  without copying a sha. **Fail-soft to `""`** — a non-repo / no-git / timeout keeps the existing
  `BLOCKED: no delivery tree_sha` path, so it never binds a bogus tree; an explicit `--tree` (e.g. the
  operator's DELIVER-GO tree) always overrides the default, so the automated gate path is unchanged. A
  single, deliberate, read-only git call — the one scoped exception to the "no git/SHA in core" convention,
  documented at the version helper.
- **Spec-derived discovery: `/config keys`, tool params on `/skills`, and a danger-grouped `/help`**
  (`engine.gx10`, epic #927 / #932): `/config keys` lists the settable dotted config keys (boot-only keys
  flagged) — closing the "opaque keys, zero discovery" gap; `/skills` now surfaces each tool's parameters
  (so `/tool <name>` is callable without reading the schema); `/help` appends the commands grouped by
  danger-tier from the command-spec. Plus a **config-set unknown-root guard**: `/config set` now REFUSES a
  key whose root section is not in the live config (a typo) instead of silently writing it and reporting a
  false success — known core sections + existing plugin namespaces (e.g. `mpr.*`) still set. `_dispatch`
  stays the untouched executor (`config keys` is an additive read-only verb, covered by the parity guard).
- **`GET /catalogue` serves the command-spec; the terminal client generates its server-command
  completions from it** (`engine.gx10._catalogue_snapshot` + `clients/ink` `catalogueToCommands`, epic
  #927 / #931): the catalogue snapshot gains a `commands` block (verb, danger-tier, usage, summary) from
  the command-spec, and the Ink client generates its server-command autocomplete FROM it — so verbs the
  static client list missed (lifecycle/fork/ace) now surface. The static `COMMANDS` list is the cold-start
  fallback for a `token`/`sealed` deployment where the guarded `/catalogue` fetch can fail (discovery + the
  alias net stay alive). Additive + fail-soft; `_dispatch` untouched.
- **Command-spec foundation — a hand-authored, parity-guarded description of the command surface**
  (`engine.command_spec` + `scripts/ci/check_command_spec_parity.py`, epic #927 Phase A / #929/#930/#940):
  a machine-readable parallel description of every slash-command verb (flags, choices, and a per-command
  danger-tier: read-only / mutating / destructive / costly, plus the six boot-only config keys). It does
  NOT drive dispatch — `_dispatch` stays the untouched fail-closed executor — it exists so the upcoming
  friendly-UX layers (discovery/autocomplete, a server-side guided-input + confirm contract, an
  alias/did-you-mean net) can be derived from ONE source. A non-vacuous spec↔dispatch parity guard derives
  the verb set from `_dispatch` source (AST over the branch literals, excluding the dynamic prompt-name +
  `else` branches), imports the real `_FROZEN_CONFIG_KEYS`, and introspects `ack.generator.build_parser` —
  failing the build on any drift (the drift that had already dropped lifecycle/fork/ace from both client
  registries).

### Fixed
- **Self-hosting bootstrap machinery complete** (epic #994 / #1013): the full set of guards for a safe
  self-modifying orchestrator is built + tested (S1–S8: the pre/post-publish orchestrator proofs, the
  ledger schema versioning, drain-before-update, the immutable protected floor, the staged-release
  guards + local rollback, the plugin backstop, and the staged-flip readiness harness), anchored by an
  operational runbook + the ADR-0004 Realized mapping + an un-testable-window fallback SOP. Executing
  the flip + the public delivery stay operator-gated. Private internal machinery — the public tool is
  unchanged (DEV-1).
- **Staged-flip readiness harness** (epic #994 / #1011): the pure guards that gate the Claude→ironclad
  orchestrator flip — `flip.stage_ready_reasons` (fail-closed readiness per Shadow/Canary/Flip stage: a
  real agent + a clean ledger chain + zero halts + enough units at the MERGE park), `agent_was_real`
  (the no-fake-agent invariant — a readiness record only counts from a REAL coder), and `select_coder`
  (pick from the OPUS/SONNET/CODEX/KIMI roster among the env-available, fail-closed on a missing
  request). Machinery only — the flip is never executed (that runs on real infra, operator-gated).
  Private dev-loop substrate.
- **Plugin backstop — consumer fail-closed** (epic #994 / #1009): the internal dev-process contain layer
  is complete — a plugin health gate that is present + resolvable but RAISES at call time is treated as
  inactive (`plugin_active` False), so `decide()` refuses and the internal loop never runs against a
  broken plugin. Complements the absent / import-error / unresolvable / falsy cases (#978/#983). The
  extension's OWN protected floor (prevent layer) is external + operator-gated. Private dev-loop substrate.
- **Staged-release guards + local rollback** (epic #994 / #1007): the machinery for a safe self-hosting
  release — `release_preflight.staging_route_ok` (a release routes through Test-PyPI first, never
  straight to production) + `main_safe_reasons` (refuse a release while a rollback is pending but main
  is not reverted, so a bad build is never re-shipped), and `scripts/ci/rollback.py` (the local
  wheel-cache recovery: pick the newest cached wheel strictly older than the bad one + a
  `--no-index --force-reinstall` pin, recovering the running instance without touching PyPI). Pure +
  tested; the actual push/release execution stays operator-gated. Private CI substrate.
- **Immutable protected floor** (epic #994 / #1005): the self-mod protected class now has a hard-coded,
  immutable `PROTECTED_FLOOR` (the engine, the guard SSOT, the CI-enforcement scripts, the delivery
  workflows, the branch-protection SSOT) that is baked into `_PROTECTED` AND re-unioned at the guard
  boundary, so protection can only GROW — never drop below the floor. A negative test pins
  `PROTECTED_FLOOR ⊆ _PROTECTED`, so a self-mod that thins it turns the gate red (structural, not just
  review); the floor also covers its own source. Private dev-loop substrate.
- **Drain-before-update guard** (epic #994 / #1003): the self-update path now refuses fail-closed while a
  unit is in-flight — a live driver holding the single-driver lock, or a unit left between BRANCH and the
  human MERGE (`drain.in_flight_units`/`update_reasons` + `run.py --drain-check`, preflighted by
  `ironclad-install.ps1`). The running orchestrator quiesces before it replaces itself, so a new engine
  never resumes half-finished state. Private dev-loop substrate.
- **Ledger schema versioning** (epic #994 / #1001): every dev-loop ledger record now carries a
  `schema_version` (`major.minor`) in its hash-protected payload; readers are forward-tolerant within a
  major (unknown fields ignored, missing defaulted, a higher minor accepted) and **fail-closed** on a
  newer major (`schema_reasons` → `run_unit` refuses `ledger-schema`), so a self-modification's ledger
  change can never break resume across versions — a non-additive change requires a migration + a major
  bump. Private dev-loop substrate.
- **Post-publish-orchestrator proof** (epic #994 / #999): the `post-publish-smoke` job now runs the same
  orchestrator-surface proof against the package installed FROM THE INDEX (test-PyPI first, then
  production) — proving the artifact users actually get ships the dev-process orchestration facade +
  fail-closes without a driver. With #997 (pre-publish) this proves the surface on exactly what ships,
  before AND after publish. Counts unchanged — #997's counted test already guards the surface.
- **Pre-publish-orchestrator proof** (epic #994 / #997): the clean-room `pre-publish-python` job now proves
  the freshly-installed wheel is orchestrator-capable — it ships the dev-process orchestration verbs
  (`ack.devprocess.api`: select_unit/stage_handover/record_feedback/advance/deliver over a driver seam)
  AND fail-closes without a driver (`SubstrateUnavailable`), plus a callable `ack.sdk.gate`. A self-mod
  that breaks or omits the surface blocks the release; a counted unit test keeps it in lock-step.
- **Health-gate wiring** (epic #974 / #983): `devtarget.plugin_active` now RESOLVES + CALLS the
  descriptor's `health_gate` (`module:function`) on top of the entry-point import check, so the internal
  process arms on the EXTENSION's own activation (the private internal-process extension's
  `tier3_activation.plugin_active`: present + operator secret + driver-wired), not merely on the package
  being importable — fail-closed on a falsy / unresolvable / raising gate. A live binding uses
  `plugin_id="internal"` + that health_gate.
- **Plugin contract seam** (epic #974 / #983): pinned + tested the `ironclad.plugins` entry-point
  contract the private internal-process extension must satisfy — `plugin_active` returns True iff the
  named entry point is present AND loads cleanly (fail-closed). The extension itself + its driver stay
  out of the public wheel and, with the C2 delivery, are operator-gated.
- **Dev-process docs + `/status` injection view** (epic #974 / #982): ADR-0011 gains an *internal
  dev-process injection* amendment (descriptor storage/lifecycle, the validation/health/fail-closed
  gates, tier-2→tier-3 migration), the stale ADR-0009 body + `tiers.py` comment are corrected (the
  engine does NOT read `config.dev_process.tier`; the shipped tool is DEV-1; per-project injection is
  the mechanism), and `/status` now shows whether the active project is an internal dev-process target
  (its exec_mode / tier / plugin) or runs the normal process.
- **The injection invariant's negative-test suite** (epic #974 / #981): a consolidated fail-closed
  suite — the **self-dogfood isolation test** (on ONE project bound as an internal target, BOTH the
  internal driver AND the normal in-engine pipeline refuse — never both) plus the full descriptor ×
  plugin decision matrix. The individual fail-closed paths stay pinned where they live (#978/#979/#980).
- **Ledger integrity across modes** (epic #974 / #980): every dev-loop transition is stamped with its
  `project_id` + `exec_mode`, and the driver refuses to start (fail-closed `ledger-fork`) if the ledger
  carries records for a different project or exec_mode — so a stale / wrong-ledger read (the
  `is_base_project` error-fallback fork the C0 review flagged) can never drive a unit twice on resume.
  Legacy pre-#980 (unstamped) records are tolerated. Private substrate; the engine still reads the ledger
  as plain data.
- **Mutual exclusion — the normal process is off on an internal target** (epic #974 / #979): a project
  bound as an INTERNAL dev-process target refuses the normal in-engine task pipeline
  (`_internal_target_blocks_normal` at stage_handover / advance / TaskStore — the internal driver drives
  it instead); `/switch` runs under a repo-global lock so concurrent switches serialize (at most one
  active mode); and the `/fork` + dev-scan exactly-once records are scoped per-project `mem_ns` (fixing
  the cross-project fork-proposal bleed the C0 review flagged). The engine reads the marker as plain
  data — it never imports the private dev-loop machinery.
- **Fail-closed injection gate** (epic #974 / #978, the core invariant): the internal dev-loop driver
  REFUSES to start on a project whose injection descriptor REQUIRES the extension when the plugin is
  not active — it never silently degrades tier-3 to the normal process on an internal target. A stdlib
  `plugin_active` health probe (is the `ironclad.plugins` entry-point present + does it load?),
  `spec.injection_refused` + a rewired `entry_plan` (a `refused`/`blocked` outcome instead of a silent
  degrade), `devtarget.decide`, and the `run_unit` pre-check wired INSIDE the single-driver lock (before
  `Driver.run`). Private substrate (not in the wheel); a negative test pins tier-3 + inactive plugin ⇒
  REFUSE.
- **Per-project injection descriptor** (epic #974 / #977): a project can be bound as an INTERNAL
  dev-process target via a runtime side-file (`<devloop_home>/dev-target.json`), SEPARATE from the
  delivery target table. The engine reads it as plain data (`_dev_target_descriptor` — never importing
  the private `scripts/devloop`) and the `/lifecycle` gate fail-closed-reconciles it against the project
  registry; the pure schema/validators (`devprocess.spec.validate_injection`/`injection_drift`) + the
  atomic registry-locked bind + reconcile CLI (`scripts/devloop/devtarget.py register|reconcile`) are
  the private substrate (not in the wheel). Foundation for the fail-closed mutual-exclusion gate (#978).
- **MPR is an embedded dev-process function, not a project type** (#984): `mpr` is removed from the
  initiative types — there is one type, `software`, which now also seeds a `runs/` home for the embedded
  MPR architecture-decision panel. The `--type` flag is dropped from `/project new` + `/initiative new`
  (a legacy `--type` is tolerated + ignored); the old reasoning-only task-pipeline gate is gone. The
  embedded MPR (the `ace.fork_mpr` architecture-decision panel + `/fork` + `run_mpr`) is unchanged, and
  `Initiative.from_meta` degrades a legacy `type: mpr` to `software` (fail-soft).
- **The orchestrator is told the real command surface** (epic #927 / #967): the engine now injects a
  spec-derived command digest — the canonical verbs + their summaries, the deprecated ones to avoid, and
  the destructive/costly ones to confirm — into the orchestrator's system context, derived from
  `command_spec` so it can't drift from the real dispatch. Fixes the class of bug the operator hit (the
  model recommended the deprecated `/initiative` and denied `/project` existed). Fail-soft + additive:
  the system-prompt file is untouched, and an empty spec injects nothing.
- **A fail-closed english-only-export guard + the last doc translations** (epic #927 / #971, part 3): the
  last German doc snippets (`state-and-initiative.md`, `SETUP.md`) are English, and
  `scripts/ci/check_english_only_export.py` now fails the build if German (umlauts or ≥2 German
  stopwords) appears in the exported `core/` + `clients/ink` + `skills/mpr` — with a documented allowlist
  for the deliberate multilingual features (intent keywords, umlaut-folding, the `messages.py`/MPR
  `locales` de-overlays, the German eval corpus fixtures, the frozen INDEX marker, tests). Wired into
  ci.yml + node-client.yml. So the whole English-only-export sweep (#966/#969/#971) can never regress.
- **The MPR skill is English** (epic #927 / #969, English-only-export part 2): the flagship MPR example
  shipped German — panel role labels (rendered to users), the tool description, module docstrings, the
  README (which also wrongly claimed MPR was "private / never exported" — it is a public core built-in),
  the eval README/harness/rubric, and `gate.toml` comments — all translated to English. The deliberate
  multilingual features stay: the German intent keywords + umlaut regexes (German-input handling), the
  de-overlay label maps, the `_STOP` set, and the German eval corpus (sets/refs/recordings) as fixtures;
  `check_coverage` now also matches an axis by its English name so English role labels cover their axis.
- **The exported orchestrator system prompt + engine/client user-facing strings are English** (epic
  #927 / #966, pre-release English-only-export pass, part 1): the entire orchestrator system prompt was
  German and shipped byte-identically to the public export; it is now translated to English (the German
  rendering is preserved as a private, non-exported `deploy/prompts/` override, selected via `GX10_PROMPT`).
  The hardcoded German TUI/CLI/ink strings (input hints, copy/cancel messages, the `/update` log, the
  command-menu hint, the auto-INDEX marker) are translated too. Deliberate multilingual features (the
  German intent keywords, umlaut-folding, the `messages.py` `de` overlay) are unchanged.
- **Guidance teaches the primary `/project`, not the deprecated `/initiative` alias** (epic #927 / #964,
  pre-release): the orchestrator **system prompt**, the client + server `/help`, the fail-closed
  `no active project` messages (EN + DE), and the docs all led with `/initiative new …` — the deprecated
  alias — so the orchestrator told an operator to run `/initiative new TEST --type software` and asserted
  "/project does not exist". They now lead with `/project new <name> --type …` (the guided setup command);
  `/initiative` is shown only as "(deprecated alias, kept one release)". A regression test asserts the
  prompt + `HELP_TEXT` + `init.no_active` recommend `/project`, not `/initiative`.
- **The confirm-before-execute and structured guided-input gates now fire on the real client wire form**
  (epic #927 / #962, pre-release): `engine.server._confirm_required` (#935) and `_guide_required` (#954)
  had required a leading `/`, but every client strips it in `classify()` before POSTing (the payload matches
  what `engine.gx10._dispatch` consumes). So the server saw `project delete <id>` / `config set ?` with no
  slash, both gates returned `None`, and the command executed — a **destructive-confirm bypass** and a dead
  `needs_guide` contract through every client (the unit tests passed because they called the gate with a
  `/`-prefixed literal). The gates now tolerate an optional leading `/` and fire on the slash-stripped form;
  a regression test ties `commands.classify(...)`'s payload to the gate so it cannot recur.

## [0.0.22] - 2026-07-01

### Added
- **`/ace eval` — efficiency diagnostic proving the ACE value-prop on the live system** (`engine.gx10._ace_command`
  + `engine.playbook_store.PlaybookStore.benchmark`, epic #855 wiring-audit follow-up / #918): `ack.ace.evaluation`
  was a complete benchmark harness only exercised by synthetic unit tests — the live system's efficiency was
  never measured. A new opt-in `/ace eval --ledger <path>` reads the ledger as plain data, projects it to
  trajectories, and runs `compare_adaptation` (ACE-delta vs full-rewrite vs evolutionary — each builds its OWN
  playbook, the live one is **not** mutated) to report the paper's verdict: J-001 (ACE does zero full-rewrites /
  LLM-merges) and J-002 (ACE cuts rollouts >50% vs the evolutionary baseline). Off the hot path, opt-in (runs
  three strategies), fail-soft, no-op without a model/ledger. **With #914/#915/#918 every ACE paper module is
  now on a live path** (`robust` in the adapt loop, `offline` via `/ace warmup`, `evaluation` via `/ace eval`).
- **`/ace warmup` — offline warm-start the playbook from a dev-loop ledger** (`engine.gx10._ace_command` +
  `engine.playbook_store.PlaybookStore.warmup`, epic #855 wiring-audit follow-up / #915): the offline
  batch-build (`ack.ace.offline`) was a complete module with no live caller. A new opt-in command
  `/ace warmup --ledger <path>` reads the ledger as plain data (boundary-clean, no private import), projects it
  via `ack.ace.devtraj.ledger_to_trajectories`, and batch-replays the historical terminal trajectories through
  `offline.warmup` to seed the active scope's playbook (which the online loop then continues on, G-004). Off
  the hot path, single-epoch by default, fail-soft, no-op without a model / scope / ledger; a chain-tampered
  ledger is blocked. Not auto-run at boot (a warm build is several LLM calls). (`ack.ace.evaluation` is wired
  separately by #918's `/ace eval`.)
- **ACE robustness half now runs in the live adapt loop** (`ack.ace.online.adapt_once`, epic #855 wiring-audit
  follow-up / #914): a post-C2 wiring audit found `ack.ace.robust` was a complete module but orphaned from
  every live path — the online `adapt_once` composed only reflect→curate→apply_delta→refine. It now runs the
  paper's K-002/K-003 self-correction **after refine**: `resolve_contradictions` (keep the higher-utility of a
  contradicted same-section pair) + `quarantine_noisy` (drop net-negative bullets), gated by `AdaptConfig.robust`
  (default on) with `quarantine_min_net` (default 0), reported as `resolved`/`quarantined` in the adapt summary.
  Off the hot path (adapt runs on the ReflectionWorker); fail-soft. `unlearn`/`version_id`/`PlaybookHistory`
  remain on-demand APIs; `ack.ace.evaluation` stays a measurement harness by design.
- **ACE playbook injection into the machine-gated dev-loop's worktree coder** (`scripts/devloop/ace_inject`,
  epic #855 M4 Mode-2 follow-up / #894): the dev-loop runs its coder as `claude --print` in a fresh worktree
  with no engine handover seam, so #863's handover injection could not reach it. Now, before the agent runs,
  the unit's relevant playbook is written into the worktree's `CLAUDE.local.md` (a Claude Code local-context
  file the coder auto-reads), added to the worktree's git exclude so it never enters the unit diff / trips the
  confinement gate, and the injected bullet ids are recorded per unit so the M4-2 ledger scan populates
  `Trajectory.used_bullet_ids` for the worktree path too. Resolves the SCOPE via the shared `project_registry`
  and reads the playbook via the engine's `PlaybookStore` (private `scripts/devloop` may import core). Fail-soft:
  no scope / no playbook ⇒ no-op (byte-identical). **Both dev-process coder modes (handover+feedback and the
  worktree agent) now receive the always-on playbook.** (Private runner change — not in the public wheel.)
- **ACE MPR-for-architecture — end-to-end proof (M5 capstone)** (epic #855 cluster ACE-FORKPROOF / #886,
  MPR-A-6): a boundary-clean e2e test of the whole gated fork propose-loop through the one core seam — a
  `ForkSignal` → the gated off-path pre-informed MPR panel (M5-2) → the matrix recorded + rendered as a
  recommendation (M5-3) → the operator resolves → a fork-decision bullet recorded (M5-4) → a second comparable
  fork's MPR query pre-informed by it. Proven for BOTH dev-processes (public generic `ack.devprocess` + the
  internal DEV-3) through the same ledger schema/seam, plus the gate-off no-op (byte-identical) and the full
  fail-soft matrix (tampered ledger / MPR no-op / MPR raises / malformed signal → the ask always surfaces, no
  crash, the loop never blocks). With M5-1..M5-5 merged, **MPR-for-architecture ships as one self-consistent,
  boundary-clean, gated (`ace.fork_mpr.enabled`, default OFF), fail-soft capability** on top of always-on ACE.
- **ACE fork-decision learning — closes the propose→decide→record→pre-inform loop** (`engine.gx10`, epic #855
  cluster ACE-FORKLEARN / #885, MPR-A-4): a newly-resolved fork (M5-1's `ForkResolution` on the ledger) becomes
  a fork-decision `ack.ace.Trajectory` — query = the fork's question (the stable comparability key), steps =
  the chosen option + area, outcome = chose <option> → <outcome>, `used_bullet_ids` = the prior fork bullets
  that seeded the M5-2 query (captured `fork:<unit>` so the Reflector rates which prior decisions helped) —
  submitted to the EXISTING background `ReflectionWorker`; reflect→curate writes a bullet into
  `strategies_and_hard_rules`, so M5-2's `context_for` pre-informs the next comparable fork. This is what makes
  M5 a *loop*, not a one-shot panel. Gate OFF (`ace.fork_mpr.enabled`) ⇒ no learning (byte-identical);
  exactly-once (a persisted decision-key set, distinct from M4-2's unit-arc + #863's per-handover completion →
  no double-learning); off-hot-path; fail-soft.
- **ACE fork proposal surface — the MPR matrix as a recommendation at the ask** (`engine.gx10` +
  `engine.playbook_store`, epic #855 cluster ACE-FORKPROPOSE / #884, MPR-A-3): the decision-matrix produced
  off-path by M5-2 is recorded as a fork-proposal pointer bound to the unit (`record_fork_proposal`/
  `read_fork_proposal`, latest-wins, bounded, boundary-clean — the full run stays under the initiative's
  `vault/<slug>/runs/`), and `_ace_fork_proposal_for(unit)` renders it as a **recommendation only** — MPR's
  top-ranked option (extracted from the synthesis) + the ranked matrix + dissent + an explicit "this is a
  recommendation, NOT a decision; you decide, ACE learns from the choice". A generic, boundary-clean seam BOTH
  dev-processes attach to their operator ask (satisfies DEV_LOOP's "ask at architecture forks, don't guess",
  now MPR-grounded, without removing the human decision). Fail-soft: a no-op MPR result (disabled/declined/
  ERROR) is not persisted ⇒ the ask surfaces unchanged, never an empty artifact.
- **ACE MPR-at-fork panel — gated, off-hot-path architecture-decision proposal** (`engine.gx10`, epic #855
  cluster ACE-FORKMPR / #883, MPR-A-2 + MPR-A-5): a new gate **`ace.fork_mpr.enabled` (default OFF)** controls
  whether a recognized `ForkSignal` (M5-1) fires MPR's existing `architecture-decision` panel — via
  `run_tool('mpr_research', {domain_hint:'architecture-decision', mode_hint:'decision'})` on a dedicated
  off-hot-path worker (a `ReflectionWorker` reused as a generic queue worker, so the multi-LLM-call panel never
  blocks the dev-loop / turn path). The MPR query is **pre-informed** by the playbook's prior relevant fork
  bullets via `PlaybookStore.context_for` (MPR-A-5). Dispatched exactly-once (a persisted fork-key set) at the
  `/lifecycle gate` ledger touchpoint. **Gate OFF ⇒ byte-identical to today's STOP-and-ask**; fail-soft on
  every mode (MPR disabled / no orchestrator model / no active initiative / RunBudget exhausted / MPR raises →
  no-op, the operator ask still surfaces). MPR only *produces* the decision-matrix here — attaching it to the
  ask is M5-3, recording the chosen outcome is M5-4. Also respects MPR's own `mpr.enabled` + `$2/run` RunBudget.
- **ACE fork signal — the architecture-decision data contract** (`ack.ace.fork`, epic #855 cluster
  ACE-FORKSIG / #882, MPR-A-1): the pure, boundary-clean foundation for the M5 *propose* layer (MPR-for-
  architecture). Defines `ForkSignal` `{unit, area, question, options[], touched_paths[]}` + `ForkResolution`
  `{unit, area, chosen_option, outcome}` with lossless `to_dict`/`from_dict`, plus a pure adapter
  (`parse_fork_signal`/`fork_signals_from` on the `FORK`/`FORK_RESOLVED` ledger surfaces) that reads a declared
  fork off the SAME boundary-clean dev-loop ledger seam `ack.ace.devtraj` (M4-1) consumes. Fork-detection is
  Variant A (declared fork at the existing STOP-and-ask point), reversible to a later Variant-B auto-detector
  reusing this exact schema. Pure/stdlib-only (imports nothing from the engine / `scripts/devloop`);
  drift-tolerant and never raises. No engine wiring / no MPR call yet (those are M5-2..M5-5).
- **ACE dev-process self-learning — end-to-end proof (M4 capstone)** (epic #855 cluster ACE-DEVPROOF / #881,
  DP-3): a boundary-clean e2e test driving the full M4 stack — ledger → `ack.ace.devtraj` → the M4-2 scan →
  the real `ReflectionWorker` → `PlaybookStore.adapt` → a real playbook mutation — for the SAME ledger schema
  both the public generic `ack.devprocess` driver AND the internal DEV-3 (`scripts/devloop`) emit, proving ONE
  adapter serves both. Also proves DP-4 (a tampered/absent/garbage ledger ⇒ no crash, no learning),
  exactly-once (a re-scan adds nothing; the per-unit merge-arc signal is distinct from #863/M4-0's per-handover
  hook → no double-learning), and the M4-3 used-bullet correlation flowing through (a used bullet is rated
  helpful, E-004). With M4-0..M4-4 merged, the dev-process self-learning loop (Variant A, ledger-derived) is
  wired + tested for the handover+feedback coder-addressing mode; the machine-gated dev-loop worktree-agent
  injection is the tracked follow-up #894.
- **ACE dev-process used-bullet correlation** (`engine.gx10` + `engine.playbook_store`, epic #855 cluster
  ACE-DEVBULLET / #880, DP-2): closes the used-bullet feedback loop for the dev-process. The handover
  injection site (#863, where the coder's handover gets the playbook via `context_for`) now DURABLY records
  which bullets it injected — keyed by the engine task id AND the issue#s the handover references (the standard
  `Closes #N` linkage; `_ace_unit_keys`/`_ace_persist_injected` → `playbook_store.record_unit_bullets`) — and
  the M4-2 dev-process ledger scan reads it back by the unit (issue#) to populate `Trajectory.used_bullet_ids`,
  so the Reflector rates which dev-loop bullets were helpful/harmful (E-004). DP-2 (the playbook reaches the
  coder) is satisfied for the **handover+feedback coder-addressing mode** — the autopilot / GitHub-only /
  file-based-with-handovers / public `ack.devprocess.stage_handover`→engine-driver flows, where the coder reads
  the handover md `context_for` injected. The machine-gated dev-loop's worktree-agent injection (no handover
  seam) is the tracked follow-up #894. `[]` when no correlation (weaker, not wrong); fail-soft throughout.
- **ACE dev-process learn-trigger** (`engine.gx10._ace_scan_dev_ledger`, epic #855 cluster ACE-DEVWIRE /
  #879, DP-1/DP-4): the engine wiring that makes the dev-loop actually LEARN. Off the hot path it scans the
  dev-loop ledger (read as plain data via the existing `_read_ledger_payloads`), projects it (`ack.ace.devtraj`)
  into per-unit Trajectories, and submits each **newly-terminal** unit (reached-human-merge-gate / aborted) to
  the existing background `ReflectionWorker` **exactly-once** (a persisted submitted-units set under
  `ironclad_home()` survives restarts). Variant A (ledger-derived): NO driver change, NO new public seam; a
  chain-tampered/unreadable ledger is **skipped fail-closed** (never a learning source); no worker/scope ⇒
  no-op. Wired into the `/lifecycle gate` command — the dev-loop's DELIVER-leg ledger touchpoint — reusing the
  payloads it already read (advisory; does not affect the gate result). A distinct per-UNIT signal from the
  in-process per-handover `post_feedback` hook (#863/M4-0): the dev-loop runner (`run.py`) is a SEPARATE
  process whose ledger writes never fire that hook, so there is no double-learning.
- **ACE dev-process trajectory adapter** (`ack.ace.devtraj`, epic #855 cluster ACE-DEVTRAJ / #878, DP-1):
  `ledger_to_trajectories` — a PURE, stdlib-only mapper that re-projects already-parsed dev-loop ledger
  payloads (the same boundary-clean `.devloop/ledger.jsonl` the engine's `lifecycle_projector` consumes) into
  one ACE `Trajectory` per unit of work: driver transitions grouped by `unit` (abort by `abort`), ordered
  timestamp-free leg summaries, and a **label-free** outcome ∈ {`reached-human-merge-gate` (the driver stops
  at the human merge gate — NOT a confirmed merge), `aborted`, `blocked`, `in-progress`}; `used_bullet_ids`
  is `[]` (M4-3 correlates the per-unit injected bullets). Variant A (ledger-derived) — imports nothing from
  the engine; schema-drift tolerant, never raises; a DELIVER record (carries no `unit`) is skipped. The
  read-half of the dev-process self-learning loop; M4-2 wires the engine learn-trigger.
- **ACE evolving-context playbook — data model** (`ack.ace`, epic #855 / ACE arXiv 2510.04618v3, cluster
  ACE-DATA): the foundation of the Loop-Intelligence → ACE extension — an itemized `Bullet` (stable id +
  helpful/harmful usage counters + categorical tags) inside a sectioned `Playbook` (the four canonical ACE
  sections + custom), with a human-readable **and** machine-parsable `render()` (explicit boundaries + bullet
  ids so a Generator can cite which bullets it used) and a **versioned, lossless JSON round-trip** (ids,
  counters, tags, and the id-minting counter all survive a reload; a newer schema version is refused
  fail-closed). Pure / stdlib-only (imports nothing from the engine). The Reflector/Curator (#858/#859),
  grow-and-refine (#860) and the **always-on** ACE-WIRE engine integration (#863) build on it — ACE is wired
  as the unconditional loop-intelligence core (no enable flag; it supersedes the #602 string-lesson +
  Process-SC consumers).
- **ACE Reflector** (`ack.ace.reflector`, epic #855 cluster ACE-REFLECT): the role that distils concrete,
  reusable insights from a reasoning trajectory + natural execution feedback (label-free) and rates which
  used bullets were helpful/harmful/neutral — structured `Trajectory` input and `ReflectorOutput`
  (candidate bullets + ratings), with optional iterative refinement over N rounds (product default 1). The
  LLM transport is **injected** (`chat`, like `verify_with_judge`) so it stays stdlib-only/clean-room-safe;
  **fail-soft** (a transport or parse error yields an empty result — never raises, never breaks the loop).
  The Curator (#859) turns its insights into deterministic playbook deltas; the async worker + budget gate
  + real transport wire in #862/#863.
- **ACE Curator** (`ack.ace.curator`, epic #855 cluster ACE-CURATE): integrates Reflector insights into the
  playbook as a compact **delta** (`Delta` = `{reasoning, operations}`, F-003) and **merges it
  deterministically** (no monolithic LLM rewrite → the ACE cost/latency win + no context collapse): `curate`
  builds add/rate ops from the Reflector output (section-validated F-002; localized per bullet C-002;
  delta-not-rewrite C-001), `apply_delta` merges them in place (add bullet / bump helpful-harmful counter +
  tag / tag / remove). Deterministic + pure/stdlib-only; robust (an op on a missing bullet, an unknown op, or
  empty content is skipped + counted, never raised); `Delta.from_json` also parses an externally-emitted
  delta so an LLM-driven Curator variant can ride the same merge.
- **ACE grow-and-refine** (`ack.ace.grow`, epic #855 cluster ACE-GROW): keeps the evolving playbook compact +
  relevant. `dedupe` merges near-duplicate bullets **within a section** — **semantic** (cosine over an
  INJECTED batched embedder at a configurable threshold, default 0.9; L-003) with a **lexical token-Jaccard
  fallback** when no embedder is reachable (sealed / no-memory) and on any embedder error (fail-soft);
  D-001. `prune` bounds the playbook by bullet-count or rendered-size budget, removing the **least useful**
  bullets first (lowest helpful−harmful, then most-harmful, then oldest; D-003/L-004). `refine` runs both,
  **proactively** or **lazily** (only over budget; D-002). Merging folds counters + unions tags into the
  surviving bullet. Pure / stdlib-only; the real model-window coordination wires in #862/#863.
- **ACE Generator integration** (`ack.ace.generator`, epic #855 cluster ACE-GEN): playbook-guided inference
  (H-001) + bullet-id usage tracking (H-002) + the Generator→Reflector closure (N-004). The key window fix:
  the playbook store may be large, but `select_relevant` retrieves only the **relevant bullet subset** per
  query (semantic via the injected embedder, lexical token-overlap fallback, fail-soft), `prepare_context`
  renders that subset (ids + counters + tags) into a `GeneratorContext` and tracks the injected ids, and
  `to_trajectory` turns the run's output + used ids into the Reflector's `Trajectory` (closing the online
  loop). Pure / stdlib-only.
- **ACE online adaptation** (`ack.ace.online`, epic #855 cluster ACE-ADAPT-ONLINE): the closed loop
  composed — `adapt_once` runs Trajectory → reflect → curate → apply_delta → refine over the playbook
  in place (label-free O-001, cumulative O-002, `rounds` L-001), **budget-gated** via an injected ledger
  (`can_afford`/`charge`, like `verify_with_judge`; skips without an LLM call when unaffordable, fail-soft
  on a flaky ledger) and a no-op-when-nothing-learned. `ReflectionWorker` is the **async seam**: `submit`
  is an O(1) non-blocking enqueue (hot-path-safe — drops on a full queue rather than block) and a background
  daemon drains it off the turn path, so the engine's `post_feedback` hook never runs the model inline.
  Fail-soft (a bad item counts an error, never kills the worker). Pure / stdlib-only; the hook wiring + real
  transports are #863.
- **ACE engine wiring — the always-on loop-intelligence core** (`engine.playbook_store` + `engine.gx10` +
  `memory-service`, epic #855 cluster ACE-WIRE / #863): a new engine-side `PlaybookStore` wraps one
  `ack.ace.Playbook` per `mem_scope` (one schema-versioned JSON file under `ironclad_home()/ace_playbooks`),
  implementing BOTH the string `ack.lessons` provider surface (`get_lessons`/`report_lesson`/`brief`) AND the
  typed `record`/`by_category`/`forget` surface the in-process Process-SC consumers couple to — plus the
  ACE-native query-aware `context_for` (the 32k-safe Generator handover read that injects only the relevant
  bullet subset) and the budget-gated online `adapt`. `gx10._apply_ace` registers it as the provider, wires a
  `post_feedback` consumer that **submits** a Trajectory to a background `ReflectionWorker` (reflect→curate→
  refine runs OFF the hot path — never inline), and starts the worker; the orchestrator-model `chat`, the
  memory-service **`/embed`** adapter (BGE-M3) and the token budget are injected. The legacy #602 lesson tree
  is migrated into the playbook on first wiring. New memory-service `POST /embed` batch endpoint exposes the
  local BGE-M3 embedder (1024-d) for semantic dedup/retrieval. Fail-soft throughout: with no orchestrator
  model reachable ACE no-ops and the playbook simply stays empty.
- **ACE offline adaptation** (`ack.ace.offline`, epic #855 cluster ACE-ADAPT-OFFLINE / #864): the offline
  counterpart to the online loop — an operator-invoked batch build over a dataset, off any turn path.
  `build_offline` runs **multi-epoch** (G-003, default 5) over **batches** (A-003, base size 1): each batch's
  per-sample reflect→curate deltas are computed **independently** (an injected `map_fn` may parallelize them)
  and **merged deterministically** (`merge_deltas`, C-003 — input order, last-wins ADD collision, RATE/TAG/
  REMOVE accumulated), then applied + refined; budget-gated, **label-free** (training labels are used only by
  `evaluate`), fail-soft per sample. `evaluate` is the test-split **pass@1** (G-001) — it needs only the
  optimized playbook, never the training data. `warmup` is the label-free batch-replay of the operator's
  execution ledger (G-004) that seeds the playbook the online loop then continues on (the same playbook is
  handed to `OnlineAdapter`). Pure / stdlib-only.
- **ACE evaluation — shipped metrics surface + comparative-baseline harness** (`ack.ace.evaluation`, epic
  #855 cluster ACE-EVAL / #865): the SHIPPED eval surface — `accuracy` (I-003, exact predicted==ground-truth
  %) and `goal_completion` (I-001 Task GC / I-002 Scenario GC — fraction successful, with a per-difficulty
  split) — plus a comparative-baseline cost/latency harness. Three BUILT adaptation strategies instrumented
  by a `RolloutMeter` so their cost is **measured**, not asserted: real ACE (`reflect→curate→merge`), a
  monolithic **full-rewrite** baseline, and an **evolutionary** validation-loop baseline. `compare_adaptation`
  proves ACE does ZERO full-rewrites + ZERO LLM merges (J-001 — local deltas + deterministic merge) and cuts
  rollouts **>50%** vs the evolutionary baseline (J-002). `kv_cache_metrics` measures the stable-prefix
  cacheable ratio of successive playbook renders (J-003 — append-only ⇒ high reuse, a rewrite ⇒ low);
  `validate_epochs` guards the adaptation-epochs hyperparameter (L-002 — ≥1, default 5); `EvalReport`
  aggregates the surface. Pure / stdlib-only.
- **ACE robustness + safety** (`ack.ace.robust`, epic #855 cluster ACE-ROBUST / #866): the mechanisms that
  make an imperfect deployment degrade gracefully + stay reversible. `quarantine_noisy` drops harmful-dominant
  (net-negative) bullets so a moderate amount of noisy/adversarial updates is rejected without collapse
  (K-002), and `adaptation_gain` shows a **weak reflector still nets positive after quarantine** while a
  stronger one gains more (K-001 — graceful degradation, monotone in reflector strength). `detect_contradictions`
  finds opposite-polarity, same-section bullets (token-overlap + negation heuristic) and `resolve_contradictions`
  keeps the higher-utility belief (K-003). `unlearn` is **selective item-level forget by bullet id** — no
  retraining, no scope-wide wipe (Q-001). `version_id` + `diff_versions` + `PlaybookHistory` (snapshot /
  rollback) are the playbook **versioning + reversible rollback** safety net (M-002). Pure / stdlib-only,
  never raises.

### Changed
- **ACE live feedback — the always-on hook now threads real used-bullet ids + a real outcome and learns from
  failures** (`engine`, epic #855 cluster ACE-LIVEFEEDBACK / #877 — **behavior change**): the handover
  injection site records which playbook bullets it injected (keyed by task_id), and the `post_feedback`
  consumer threads those **real `used_bullet_ids`** + a **label-free outcome** into the Trajectory — a fresh
  `OK: pipeline advanced` ⇒ `success`, an `ERROR: pipeline step failed` ⇒ `failed`. So the Reflector now
  learns from FAILURES too (E-001/O-002) and rates the bullets the task actually used (E-004/H-002), feeding
  grow-and-refine's utility prune (D-001/D-003) — the machinery #863 stood up but never exercised on the live
  general-task path (it previously hardcoded `outcome="success"` + empty ids and fired only on success).
  Already-done re-advances + trivial precondition errors are skipped; off the hot path + fail-soft preserved.
- **ACE is now the always-on loop-intelligence core, superseding the #602 string-lesson + Process-SC
  consumers** (`engine`, epic #855 / #863 — **behavior change**): ACE has **no enable flag** — `_apply_ace`
  always registers the `PlaybookStore` as the `ack.lessons` provider and the ACE `post_feedback` consumer,
  **replacing** the #602 `_apply_lessons_provider`/`_apply_lessons_consumer` (#804) and `_process_consumer`
  (#803) wiring. Process-SC's distillation is subsumed by `reflect→curate`, but its TYPED read surface never
  silently breaks: `_concrete_lesson_provider` is now **duck-typed** (a `record`/`by_category` capability
  probe, not an `isinstance` check), so it resolves to the `PlaybookStore`. The handover read-site injects the
  query-aware playbook context (`context_for`) instead of the flat `brief`. A foreign extension provider is
  never clobbered (richer-wins: ACE steps back while one is registered). The earlier default-off /
  byte-identical contract for the loop-intelligence layer is intentionally retired — ACE is the engine's
  loop-intelligence mechanic.
- **Lifecycle gate `tests`/`reviews` evidence is now produced, and an inert review is excluded** (`engine`,
  #601 S13b / #632 follow-up #830): the dev-process driver's transition `log` seam is wired to the
  per-project ledger (`build_real_ops(ledger_path=…)`, threaded from `run.py`), so **every** transition —
  not just the DELIVER record — is appended. A green composed GATE projects to the `tests` stage and an
  **enforced** review-evidence leg to `reviews`, so `/lifecycle gate --stages tests,reviews,delivery` is
  now enforceable (the default stays the conservative `delivery`). A dry-run / non-live review is recorded
  with an `(inert)` marker and is **excluded** from the `reviews` stage, so reviews evidence requires a real
  review. All ledger consumers (merge-walk / published-issues / Test-PyPI-first guard) filter on
  `surface == "DELIVER"`, so the added transition records are inert to them and the merge-walk high-water
  mark stays intact.
- **Handover reasoning effort is auto-tiered by task class** (`engine`, #500 / #456 follow-up): when a
  staged handover carries no explicit `effort:`, the autopilot launch now derives it from the deterministic
  task class — **security / architecture → `xhigh`**, **routine (coding / analysis) → `high`** — instead of
  the flat default. An explicit handover `effort:` still wins; an unmapped class or an unreadable task falls
  through to the previous spec/default chain unchanged (**fail-open**). Token-balancing: the hardest tasks
  automatically get more reasoning budget without relying on the operator/method setting `effort:` by hand.

### Fixed
- **`/chat` response no longer carries interactive pane chrome** (`engine.server._strip_chat_chrome`,
  desktop functional-acceptance finding / #921): a captured `POST /chat` turn returned the `[GX10]` and
  `[Qwen (planning)]` / `[Qwen (running)]` status markers mixed in with the answer. These are terminal-pane
  affordances, not part of the answer — the server now strips them (and collapses the blank lines they leave)
  from the buffered response, keeping the answer plus the perf/DONE status. The interactive terminal and the
  `/chat/stream` path are unchanged. Fail-soft (returns the text as-is on any error).
- **Ink FPS micro-benchmark no longer false-fails under machine load** (`clients/ink/test/fps.test.tsx`,
  desktop functional-acceptance finding / #920): the benchmark asserted a wall-clock 30fps budget
  (<33ms/frame), which is timing-sensitive — on a busy machine it measured ~177ms/frame and failed, a false
  red unrelated to the renderer (production coalesces token updates to maxFps). The wall-clock assertion is
  now gated behind `INK_PERF=1` (an idle machine / a dedicated perf run); by default the test still exercises
  the full render path (100 rapid rerenders) and logs the timing, plus an always-on functional smoke that the
  live region still renders content after the rerenders.
- **ACE Reflector now runs thinking-OFF so the always-on loop learns on a reasoning model** (`engine.gx10.
  _ace_chat_adapter`, desktop functional-acceptance finding / #922): the Reflector's orchestrator-model call
  emits STRUCTURED JSON (insights/ratings), not free reasoning, but it was sent WITHOUT disabling qwen3
  thinking and with only a 1024-token cap. On the deployed reasoning model (qwen3.6-35b) the call burned the
  whole budget on a `<think>` block (`finish_reason=length`) and returned EMPTY `content` → 0 insights → the
  always-on playbook NEVER mutated (the headline capability was inert on the production model — a live-only gap
  the stub-transport unit tests miss). Now the adapter passes `extra_body={'chat_template_kwargs':
  {'enable_thinking': False}}` + a 2048-token budget (mirroring MPR's classify/`complete_json`). Live-verified:
  ACE now distills a real bullet from a trajectory on qwen3.6-35b. Regression test asserts the thinking-off +
  budget request params.
- **ACE S4 polish — KV-cache stable prefix, docstring, no multi-issue cross-attribution** (`ack.ace.playbook`
  + `engine.playbook_store` + `engine.gx10`, epic #855 C2 findings / #906): `Playbook.render()` now trails the
  mutable `(↑helpful ↓harmful)` counters AFTER the stable `[id] content #tags` (KV-cache stable prefix, N-002);
  `get_lessons`' empty-query docstring corrected ("recency" → net_utility with a recency tiebreak, matching the
  code); and `_ace_unit_keys` keys only the FIRST `#N` in a task title (the primary unit) so a title that also
  mentions another issue no longer cross-attributes injected bullets (a `Closes #N` body linkage stays
  deliberate). The J-001 hardcoded-zero rewrite metric (ACE genuinely does zero full-rewrites — already
  documented) and the rare abort-then-recover mislabel (arc preserved in `steps`) were reviewed and accepted
  as-is.
- **ACE learning robustness — top_k wiring, exactly-once crash window, contradiction modals, foreign clobber**
  (`engine.gx10` + `engine.playbook_store` + `ack.ace.robust`, epic #855 C2 findings / #905): four S3 nits.
  (1) `ace.top_k` was inert — `_apply_ace` never threaded it to `context_for`; it is now applied via
  `PlaybookStore.configure(top_k=…)` so `/config set ace.top_k N` actually changes the injected-bullet cap.
  (2) The M4-2 dev-process exactly-once key is persisted per-item BEFORE `worker.submit` (at-most-once), so a
  crash between submit and the previously-batched save can no longer re-learn a unit. (3) `detect_contradictions`
  no longer treats the bare modals "should"/"must" as negations (obligation, not polarity), removing
  false-positive contradiction deletes. (4) The always-on supersede check is identity-based
  (`current is _ACE_STORE`), so a foreign PlaybookStore-typed provider is never clobbered.
- **ACE fork MPR dispatch is crash/drop-safe + tears down on gate off** (`engine.gx10`, epic #855 C2 finding /
  #904): the exactly-once fork key was persisted at dispatch, so a full-queue drop or a worker crash
  permanently lost the proposal with no retry. It is now committed only **after** the MPR run completes (an
  in-memory in-flight guard prevents concurrent duplicate dispatch; a dropped/crashed run stays un-committed
  and is retried on the next scan). The dedicated fork worker is now also torn down (stopped + cleared) on a
  gate ON→OFF flip, so `ace.fork_mpr.enabled off` leaves no stranded idle daemon.
- **ACE MPR fork proposal is now surfaced to the operator via `/fork`** (`engine.gx10` + `engine.playbook_store`,
  epic #855 C2 finding / #903): M5-3 recorded + rendered the decision-matrix but `_ace_fork_proposal_for` had
  no production caller, so MPR-A-3's output leg was inert. A new read-only **`/fork [unit]`** command
  (`_fork_command` + `playbook_store.list_fork_proposals`) lists the pending architecture proposals (or renders
  a single one) as a **recommendation only** — the boundary-clean, both-dev-process seam where the operator
  sees the MPR matrix at a fork and decides (ACE then learns the choice, M5-4). Fail-soft; no proposal ⇒ a
  clear "none pending" note.
- **ACE playbook prune evicted the newest bullet instead of the oldest on a tie** (`ack.ace.grow.prune`, epic
  #855 C2 finding / #902): the overflow-prune tiebreak used `-_seq(b)`, so on a `net_utility` + `harmful_count`
  tie `min()` picked the largest seq (newest) — contradicting the documented "oldest (D-003)" contract and
  discarding the freshest lesson. Corrected to `_seq(b)` (oldest first); regression test added.

## [0.0.21] - 2026-06-30

### Added
- **Curated-global memory tier + per-`mem_ns` reflection** (`memory-service`, #601 S15 / #634 / ADR-0011
  AD-4·AD-9): a SEPARATE physical Qdrant `curated_global` collection (its own Mem0 instance — 0.1.118 binds the
  collection at construction) for operator-promoted, redacted, cross-project knowledge. An operator-gated
  `POST /promote` (fail-closed: `confirm` + a source `mem_ns` + exactly one of redacted-text / source-query)
  writes ONLY into `curated_global`, never `agent_memory`; an OPT-IN `/search?include_curated` fans it in with
  **project-wins** precedence. Being a separate collection it never appears in `/scopes` or the orphan-GC, by
  construction. Reflection is now **per-`mem_ns`** (was one global counter+lock): the threshold-fire counts +
  the graph-hygiene Cypher are scoped to the firing partition (`n.agent_id`), with an OPTIONAL Valkey backend
  (`MEM0_WARM_URL`) for an atomic multi-worker counter (`INCR`) + interprocess lock (`SET NX`) — **fail-soft**
  to the file + in-process per-scope mechanism (correct at the single uvicorn worker the image runs). The
  non-lossy `combine` merge is opt-in via `REFLECT_MERGE_PROPS` (default `discard`, byte-identical). Pure logic
  offline-tested (`curate.py`).
- **Lesson completion-write re-homed onto the Hook-Bus** (`gx10`, epic #602 2.3 / #804): the task-completion
  lesson write (#601 S14-4) is now driven by a `post_feedback` Hook-Bus consumer (`gx10._lessons_consumer_hook`,
  registered on **provider presence** via `_apply_lessons_consumer` — mirroring the inline write it replaces,
  which fired whenever a provider was wired) through the real `_advance_pipeline` wrapper, **outside the vault
  lock**. With #803 this puts **both** completion-writes (Process-SC + Lessons) on **one consistent reflection
  wiring path**. The consumer gates on a fresh completion (so an already-done re-advance does **not**
  double-report), reads the archived feedback only when a provider is wired, and stays **fail-soft**. Default
  (no provider) → no consumer registered → **byte-identical** no-op. Covered by `test_lessons_rehome_wiring.py`.
- **Process-Level Self-Correction re-homed onto the Hook-Bus** (`gx10`, epic #602 2.2 / #803): the Process-SC
  completion write is now driven by a `post_feedback` Hook-Bus consumer (`gx10._process_consumer_hook`,
  registered per `process.enabled` via `_apply_process_consumer`) through the real `_advance_pipeline` wrapper —
  re-homed from the inline call in `_advance_pipeline_impl` onto the bus, **outside the vault lock**, so the
  reflection consumers share **one consistent wiring path**. The consumer gates on a fresh completion (so an
  already-done re-advance does **not** double-record), keeps `_record_process_lesson`'s own `process.enabled` +
  concrete-provider + bound-scope gates, and stays **fail-soft** (a raising provider never breaks the advance).
  Default OFF → no consumer registered → **byte-identical** no-op. The `pre_turn` hint (`_process_hint`) stays a
  direct prompt-assembly call (the observer-only bus cannot inject prompt content). Covered by
  `test_process_rehome_wiring.py`.
- **Closed-loop Loop-Intelligence — proven end-to-end + 8b wired** (`gx10`, epic #602 2.x / #809, the C2
  done-gate): a CI-enforced closed-loop e2e (`test_closed_loop_e2e.py`) drives the whole reflection loop on
  the dev-task pipeline in one run and asserts **every consumer fires** (no link is a no-op — the test the C1
  half-ship would have failed): a staged handover is scored (Verifier) → the score feeds the Quality breaker
  and trips it → a run failure is classified (FailureClass) → the Strategy Revisor escalates on a spent budget;
  with all flags off the whole path is a **byte-identical no-op**. Also **wires 8b**: `LoopProfile.eval_verifiers`
  (the per-type `eval` key) now **selects which mark-only verifiers the Verifier runs** (`gx10._verifier_hook`;
  empty → the default rules+grounding; the async LLM-judge stays a separate opt-in). And folds the carried
  review nits: the Quality consumer **feeds the verdict once** (clears it after recording, no stale re-feed) and
  **surfaces a trip only on the not-tripped→tripped transition**; the Verifier reads `verify.grounding_threshold`
  from an **apply-time flag** (`_VERIFY_GROUNDING_THRESHOLD`) instead of `_EFFECTIVE_CFG`. **All `docs/status.md`
  reflection rows now read `wired + tested` — no `delivered (seam)` / `no live consumer` / `not yet consumed`
  wording remains.** This completes the functional Loop-Intelligence layer (#602 C2). Covered by
  `test_closed_loop_e2e.py` (3).
- **Per-TaskType loop profile on the dev-task pipeline** (`gx10`, epic #602 2.6 / #807): the first live
  `loop_profiles.by_type` consumer. The code-agent failover escalation budget (#806) is now resolved **per the
  staged task's type** — `gx10._failover_budget` runs `resolve_loop_profile(by_type[<type>].retry_budget)`
  layered over `strategy.budget` (the default) and clamped to the hard re-ask ceiling, so a per-type override
  can only **lower** the budget (a `chat`/`bug` type escalates sooner). Empty `by_type` → the default budget →
  **byte-identical** to the flat #806 budget; opt-in per `strategy.enabled`; fail-soft (any store/resolver
  hiccup → the default). The chat loop's `max_iterations` keeps the default profile (it has no per-task type).
  Also tightens the `strategy.budget` clamp to reject a `bool`. Covered by `test_loopprofile_pertype.py` (3).
- **Strategy Revisor consumed at the code-agent failover** (`gx10` + `engine/server.py`, epic #602 2.5 / #806):
  the engine now consults the pure failure→action policy on a code-agent run failure instead of an endless
  silent failover. `gx10._revise_on_failure(task_id, result)` runs `providers.code_agent_strategy` per task —
  a per-task attempt counter vs the new `strategy.budget` (default 3) — and **surfaces a `HUMAN_ESCALATION`**
  (`gx10._last_strategy()` + a `strategy` field in the `/feedback` response + a `[strategy]` line) when the
  budget is spent; a successful run resets the task's counter (so it acts on the fresh failure, never a stale
  class). **OPT-IN** per `strategy.enabled` (default OFF → byte-identical); **MARK-ONLY** — it surfaces, it
  never adds a new hard-abort. The richer re-ask actions (inject-context / switch-retrieval) ride
  `validated_emit`'s `strategist` in the MPR plugin, not the code-agent failover. Covered by
  `test_strategy_failover.py` (4).
- **FailureClass produced at the code-agent failover** (`gx10` + `engine/server.py`, epic #602 2.4 / #805):
  when a code-agent run is classified as failed/unavailable on the `/feedback` path, the engine now maps it
  onto the shared `FailureClass` (`providers.result_failure_class`) and **records** it
  (`gx10._record_failure_class` → `gx10._last_failure_class()`) + **surfaces** it as a `failure_class` field in
  the response — so the Strategy Revisor consumer (#602 2.5 / #806) can act on *why* a run failed. **OPT-IN per
  a new `strategy.enabled` config key** (default **OFF** → nothing recorded, no response field → byte-identical;
  captured at config-application time via `_apply_strategy`, so it works through `_apply_config` not only the
  config-tree loader). **Fail-soft** — classifying a failure never breaks the feedback path. Covered by
  `test_failure_class_failover.py` (4).
- **Quality breaker consumes the Verifier scores** (`ack.quality` + `gx10`, epic #602 2.7 / #808): the
  output-quality circuit breaker is now **fed on the default path** — a `post_handover` consumer
  (`gx10._quality_consumer_hook`, registered per `quality.enabled` via `_apply_quality_consumer`; default
  **OFF** → no hook → byte-identical) records the mark-only Verifier score (`gx10._last_verdict()`) into the
  breaker on every staged handover and **surfaces** a sustained-degradation trip (`gx10._quality_tripped()` +
  a `[quality]` advisory line). **MARK-ONLY** — a trip is advisory (escalate/surface), never a hard-abort;
  **fail-soft**. This closes the **Verifier → score → breaker** segment of the loop. Also **hardens the #802
  Verifier**: grounding now runs in its own fail-soft block (a memory hiccup drops only grounding, never the
  already-computed rules verdict) and caps the per-claim cold-store lookups. Covered by
  `test_quality_consumer_wiring.py` (4) + the verifier grounding-error case in `test_verifier_wiring.py`.
- **Verifier wired on the dev-task pipeline** (`ack.verify` + `gx10`, epic #602 2.1 / #802): the mark-only
  behavioral Verifier now **runs on the default path** instead of being a dormant seam. A `pre_handover`
  Hook-Bus subscriber (`gx10._verifier_hook`, registered per the new `verify.enabled` config key — default
  **OFF** → no hook registered → byte-identical) evaluates each staged task: deterministic **behavioral rules**
  over `task_json` (beyond-schema quality) + (when a memory tier is up) **grounding** of the handover's claims
  against the cold store. It stores a mark-only `VerdictResult` (`gx10._last_verdict()`) for the Quality breaker
  (#602 SUB-9) to read. **MARK-ONLY** — it never gates a handover; **fail-soft** — a Verifier hiccup never
  breaks staging. The opt-in **LLM-judge** (`verify_with_judge`) remains a separate explicit activation (it
  charges the budget ledger) and is not run by this hook. `ack.hooks` gains **`unregister_hook`** (identity-
  based) so an opt-in consumer can deregister cleanly on disable without clobbering sibling hooks. Covered by
  `test_verifier_wiring.py` (6) + the `unregister_hook` cases in `test_hooks.py` (4).
- **Loop-Intelligence Hook-Bus** (`ack.hooks`, epic #602 SUB-2 / Teil-2 plan 2.0 — the keystone): a
  standalone, dependency-inverted, fail-soft event bus over the agent-loop boundary points, so the reflection
  consumers (Verifier / Quality / Process-SC / Lessons) subscribe instead of hard-wiring call-sites into
  `gx10`. The engine **publishes** seven canonical events — `pre_turn`, `post_generate`, `post_toolresult`
  (from `run()`) and `pre_handover`, `post_handover`, `pre_advance`, `post_feedback` (from the
  `_stage_handover`/`_advance_pipeline` wrappers, outside the cross-process vault lock). `register_hook` is
  additive + idempotent and fail-loud on an unknown event / non-callable; `dispatch` is **observer-only** (a
  hook may ABORT but can never relax/permit a gate), **fail-soft** (a per-hook exception is swallowed),
  cancel/budget-aware, and copy-on-write + snapshot-on-dispatch for the multi-threaded engine. **Default-off
  byte-identical**: with no hook registered `dispatch` is an O(1) no-op, so the agent loop is unchanged.
  `HOOK_EVENTS` + `register_hook`/`dispatch`/`clear_hooks`/`registered_events`/`hook_count` join the public ACK
  surface. Covered by `test_hooks.py` (15) + `test_hook_wiring.py` (5, engine publish-point integration).
- **`/health` surfaces the project-registry binding** (epic #710, sweep): `/health` now carries a `registry`
  block — `status` (`ok`, or `unisolated` when the engine fell back to the un-isolated mode at boot — a
  fallback previously only logged once), the active project `id`, and the installation-global `home`
  (`GX10_HOME`). `gx10.registry_health()` computes it fail-soft (never raises), and the desktop
  `ironclad-doctor.{sh,ps1}` print it — so the otherwise-invisible project-isolation binding is observable
  after boot. Additive; no behavior change.
- **Process-Level Self-Correction** (`ack.process`, epic #602 S602-6): correct the *workflow*, not the
  response. A **pure** policy — `distill_process_lesson(ProcessSignal) → ProcessLesson` (a successful task →
  a reusable working-path note; a missing clarification → a gather-up-front note; nothing actionable → None)
  and `format_process_hint(texts)` (a compact pre-turn block) — both deterministic and never-raising. The
  engine wires it OPT-IN: at task completion (`post_feedback`) it distills a TYPED process-lesson and stores
  it via the **concrete** `EngineLessonStore` (`record`/`by_category` — not the string-only `ack.lessons`
  seam, which can't round-trip typed fields), and before the next turn (`pre_turn`) it folds known
  working-approaches into the prompt prefix alongside RAG/steer. Gated on a new `process.enabled` config key
  (default OFF → nothing recorded, no hint → **byte-identical**) and a registered concrete provider; fail-soft
  throughout, never mutates the fail-closed path. `ProcessSignal` / `ProcessLesson` / `ProcessLessonKind` /
  `distill_process_lesson` / `format_process_hint` join the public ACK surface. Covered by `test_process.py`.
- **Quality Circuit Breaker** (`ack.quality`, epic #602 S602-9): a **separate**, agent-agnostic per-task
  output-quality breaker (`QualityBreaker`) — distinct from the per-peer availability breaker
  (`_CODE_AGENT_BREAKER`); folding quality into that would corrupt code-agent failover. It tracks the trend of
  the mark-only verifier scores (`ack.verify`) and trips on **sustained degradation** (`min_consecutive`
  scores in a row below `threshold`; an at/above-threshold score resets the streak). **MARK-ONLY**: a trip is
  advisory — the consumer escalates / surfaces to the operator (pause-autoplan is opt-in), **never a
  hard-abort**, and the fail-closed core is untouched. **Fail-open-safe**: every method never raises and any
  hiccup leaves it untripped. Wired OPT-IN behind a new `quality.enabled` config key (default OFF → no breaker
  built → no-op byte-identical); `_apply_config` builds/clears the engine's `_QUALITY_BREAKER` (separate from
  the availability breaker), `_quality_breaker()` exposes it. `QualityBreaker` / `QualitySnapshot` join the
  public ACK surface. Covered by `test_quality.py`.
- **Verifier / Evaluation Layer** (`ack.verify`, epic #602 S602-4): a **mark-only** behavioral-evaluation
  seam — `VerdictResult` (passed / score / reason) from three opt-in, transport-injected, secret-free
  verifiers: `verify_rules` (deterministic business-logic predicates), `verify_grounding` (each claim grounded
  by an injected `retrieve`, e.g. a cold-store hit), and the async `verify_with_judge` (LLM-as-judge over the
  injected `chat`, **budget-gated** — it charges an injected ledger duck-typed on the engine's
  `dispatch.BudgetLedger` and SKIPS the call when unaffordable). **MARK-ONLY**: a verdict can neither relax nor
  tighten any gate — the fail-closed core is untouched; verdicts are read only by the opt-in reflection layer
  (the Quality breaker #602 SUB-9). Every verifier **never raises** (an error abstains/fails advisorily) and is
  **default-off byte-identical** (nothing runs unless invoked). Plus **8b — per-profile eval activation**: a
  `LoopProfile.eval_verifiers` resolved from an `eval` key in the loop profile (which verifiers a consumer runs
  per `TaskType`; empty by default → byte-identical). `VerdictResult` + the verifiers join the public ACK
  surface. Covered by `test_verify.py` (+ `test_loop_profile.py`).
- **Loop Profiles** (`ack.loop_profile`, epic #602 S602-8a): per-`TaskType` loop budgets — a **pure**
  `resolve_loop_profile(...)` that deep-merges code defaults ← `loop_profiles['default']` ←
  `loop_profiles['by_type'][<type>]` into a `LoopProfile` (max_iterations / retry_budget / effort); only
  present keys override, `retry_budget` is clamped to the hard re-ask ceiling, and it **never raises**. The
  schema + defaults live in the engine config tree (a new `loop_profiles` block, **empty by default** → the
  resolver falls back to the engine globals `MAX_ITERATIONS` + the re-ask budget → **byte-identical** and
  single-sourced); the engine accessor `_loop_profile(task_type)` drives the chat loop's iteration bound (the
  default profile, == `MAX_ITERATIONS` unless configured). An operator (or the private monorepo's override
  layer) can raise/lower limits per task type without touching `core/`; the public clean-room default is
  unchanged. `LoopProfile` / `resolve_loop_profile` join the public ACK surface. (8b — per-profile eval-gate
  activation — follows the Verifier.) Covered by `test_loop_profile.py`.
- **Strategy Revisor** (`ack.strategy`, epic #602 S602-7): a **pure** failure→action policy
  `revise(failure_class, attempt, budget) → Strategy` (the SSOT) that turns a `FailureClass` into a *targeted*
  next move (`StrategyAction`: inject-context / narrow-or-clarify / switch-retrieval / ground-then-answer /
  complete-output / resolve-policy / repair-schema / fail-over / human-escalation) instead of retrying the
  same way — escalating to a human when the budget is spent. Two opt-in application seams consume it:
  `ack.validated_emit.emit_validated` gains an optional `strategist` parameter that appends the strategy hint
  to the re-ask turn (**byte-identical when not passed** — default `None`; a strategist error is swallowed),
  and the engine-side `providers.code_agent_strategy(result, …)` maps a code-agent run result through the same
  SSOT for the failover path. `Strategy` / `StrategyAction` / `revise` join the public ACK surface. Pure +
  deterministic (snapshot-tested), `revise` never raises. Covered by `test_strategy.py`.
- **Project-private lesson distiller** (`engine.lesson_store.EngineLessonStore`, epic #602 S602-5): the one
  concrete `ack.lessons` **provider** the engine registers — supplying the lesson *semantics* the #601 seam
  delegates to: typed distiller categories (last-failure-reason / best-known-path / known-bad-strategy /
  user-preference — provider-internal; the public seam stays string-only), query (term-overlap) + recency
  ranking, per-scope compaction, a **scope-keyed persistent backend** (one JSON file per opaque `mem_scope`
  under `ironclad_home()/lessons`, hashed to a filesystem-safe name — never the global WARM session, and it
  imports nothing from `engine.memory`/`engine.warm`), and the optional `forget(scope)` purge so the engine's
  scope-aware forget actually drops a project's lessons. **OPT-IN: a new `lessons.enabled` config key, default
  OFF** → no provider is wired and the seam stays a **byte-identical no-op**; `_apply_config` registers the
  store when on (and clears only our own store when off, never clobbering a foreign provider). C1 = project-
  private lessons only (the global `user_preferences` tier is deferred — it needs the curated-global store +
  a `promote()` redactor). Fail-soft throughout (a corrupt/missing scope file reads as empty). Covered by
  `test_lesson_store.py`.
- **Shared failure-classification taxonomy** (`ack.failure_class`, epic #602 S602-3): a single,
  string-valued `FailureClass` enum (MISSING_CONTEXT / BAD_TOOL_ARGS / RETRIEVAL_FAILURE /
  HALLUCINATED_ASSUMPTION / INCOMPLETE_OUTPUT / POLICY_CONFLICT / SCHEMA_INVALID / UNAVAILABLE) so every
  reflection consumer names *why* a step failed in one place — generalizing the 3-class code-agent run
  taxonomy. `classify_emission_failure(message, detail)` is a **pure, deterministic, rule-based** mapper over
  the exact error the validated-emit re-ask loop already records (no model, never raises); the engine
  `providers.result_failure_class` re-maps the run results onto the same enum (one SSOT). The loop now attaches
  an **additive, advisory** `ValidatedEmitResult.failure_class` on a terminal failure — `None` on success and
  on any result built without it, i.e. **byte-identical** to the pre-#602 shape; it never affects control flow.
  `FailureClass` + `classify_emission_failure` join the public ACK surface (advisory labels, **not** the
  contract-SSOT). Covered by `test_failure_class.py`.
- **Base-untouched reconciler check** (epic #601, S17 / AD-8): a full project lifecycle (mint → switch →
  stage a unit → delete) run under a throwaway working dir leaves the engine's own **delivered source
  surface** (`core/skills` + `engine/prompts`) **byte-identical** — a content-hash snapshot before/after
  (bytecode caches excluded) catches any regression where a path resolves into the engine tree instead of the
  project root. (The live counterpart — the installed engine + the private `conf/` byte-checked after a real
  dev cycle — is the operator-gated deploy step.) Negative-test coverage of the new private modules is
  satisfied by construction: each shipped with fail-closed / error-path tests under its per-PR adversarial
  review. Covered by `test_base_untouched.py`.
- **Self-dogfood isolation acceptance (offline)** (epic #601, S17 / AD-8): a deterministic acceptance test
  that drives the real engine surface — `/project new` → `/switch` → stage a unit of work → switch back — for
  two projects through the actual quiesced-switch machinery (no live infra, no model/agent, no gh/PyPI
  deliver), and asserts the whole-epic invariant: each project's vault, state machinery, and memory partition
  are isolated under its own root; work artifacts live only under the active project; the implicit
  base/`default` project is never touched; and switching never bleeds the conversation. The live self-dogfood
  run (a real separate checkout, a real run→deliver) is the operator-gated deploy step. Covered by
  `test_self_dogfood_acceptance.py`.
- **Export carries no project state** (epic #601, S17 / AD-8): the publish export is asserted free of any
  runtime project-isolation artifact — the per-project engine machinery (`.ironclad/`), per-track vault
  subtrees (`.tracks/`), and the installation-global project registry (`registry.json`) are all created under
  a project root at runtime and never belong in the source export. They are excluded from the copy and, as a
  fail-closed backstop, `scan_project_artifacts` fails the export if any are found in the staged tree. (Part
  of the epic's acceptance gate; the live self-dogfood cycle + base-untouched reconciler land with the
  operator-gated deploy.) Covered by `test_export_no_project_artifacts.py`.
- **Project lifecycle — `/project delete` + `/project archive`/`unarchive`** (epic #601, S16): registry-
  mediated removal + a reversible archived flag. `delete <id> [--purge]` **forgets every memory scope** the
  project owns (cold + warm + lessons, the main track and each parallel track) **before** removing the
  registry entry; the on-disk directories are **left untouched unless `--purge`** is given — and `--purge` is
  guarded against deleting the cwd / boot workdir / home / any ancestor of them. Deleting the **active**
  project first switches to `default` (a clean unbind via the quiesced switch), so the engine is never left
  bound to a deleted project; the `default` project is never deletable. `archive <id>` / `unarchive <id>`
  toggle a reversible `archived` flag (data + memory kept): archived projects are hidden from `/project list`
  (shown with `--all`), refused as a `/switch` target, and the active/default project cannot be archived.
  Covered by `test_project_delete_archive.py`.
- **Guided project setup — `/project new <name> [--type mpr|software] [--path <dir>]`** (epic #601, S16):
  `/project` becomes the home of the guided setup command. `new` now **mints a fresh isolated project** in one
  step — registers it (root = `--path` or `<cwd>/<slug>`, a minted `mem_ns`, made active), binds the engine to
  it, and (with a `--type`) **seeds the first vault unit** via the existing initiative machinery, then reloads
  the per-project library. Fail-closed on a duplicate root or an unknown `--type` (nothing partial). The old
  bare `project new <id> <path>` register-only form is replaced by this richer mint (a project name, not an
  id+path pair). `/initiative` is now a **deprecated alias** (kept functional for one release, with a
  deprecation notice) — new work flows through `/project`. Covered by `test_project_mint.py`.
- **Project track verbs — `/project track new|use|list`** (epic #601, S16 / AD-2'): manage a project's
  parallel **tracks** from the CLI. The registry gains `add_track` (idempotent, fail-closed on an unsafe id
  or unknown project) and `set_active_track` (fail-closed when the track isn't registered). `track new <t>`
  **creates and switches** (like `git checkout -b`), `track use <t>` switches to an existing track, and
  `track list` shows them with the active one marked. Switching rebinds the engine context and reloads the
  per-track library, so a non-`main` track's vault subtree (`.tracks/<t>/`) and memory sub-scope
  (`<mem_ns>::track::<t>`) take effect immediately; `main` stays byte-identical. (First slice of the unified
  guided setup command — the `/project new` mint pipeline + wizard and `delete`/`archive` follow.) Covered by
  `test_track_verbs.py`.
- **Memory-service scope isolation + registry-keyed orphan GC** (epic #601, S15 / AD-4): the safe slice of
  the memory-service capabilities. The memory service gains a pure `scope_guard.require_scope` (stdlib-only,
  unit-tested offline) that **requires `agent_id` (the `mem_ns` partition)** on `/add`, `/add_bulk`, and
  `/search` (an unscoped write/search is refused) and **rejects `run_id` as an isolation key** (the partition
  is `agent_id` only). A new `GET /scopes` lists the distinct partitions present in the store. On the engine
  side, `MemoryManager.list_scopes()` reads that listing (fail-soft), `gx10._orphan_scopes` flags only
  **minted** `mem_ns` partitions with no registered project (so the base partition and curated/human-named
  scopes are never touched, and a track sub-scope is judged by its project key), and
  `gx10._reconcile_orphan_memory` forgets the orphans via the scope-aware forget — **dry-run by default**
  (destructive opt-in), fail-soft per orphan. The deeper memory-service items (a separate physical curated
  Qdrant collection + per-`mem_ns` non-lossy reflection) are deferred to the deploy/acceptance step, where
  the live stack can verify them. Covered by `test_scope_guard.py` + `test_orphan_gc.py`.
- **Scope-aware forget + scope tagging** (epic #601, S14-5 / AD-10 / D5): the substrate now has a delete
  path that targets a single project/track partition. Cold writes (`store_task_completion` / `add_bulk` /
  `chunk_and_store`) **self-describe their origin `scope`** in the stored metadata (for promotion eligibility
  + cross-partition audit), added **only when a project scope is bound** so the base partition stays
  byte-identical. `MemoryManager.forget(scope)` forgets a whole partition via a new Mem0 `/delete_all` route
  (added to the memory service; deletes by `agent_id`, from both the graph+vector and vector-only stores) —
  synchronous, **fail-soft** (a down service / missing route returns `False`), **fail-closed on an empty
  scope**. The warm tier's `WarmTier.forget_scope(scope)` deletes the **exact**-scope `session:` + `ret:`
  (retrieval-cache) keys and deliberately does **not** cascade into deeper track scopes (`…::track::x`),
  fail-closed on an empty / glob-bearing scope. `ack.lessons.forget(scope)` is an **optional** provider verb
  (delegated when the provider implements it, else a no-op — kept off the required `LessonProvider` protocol).
  `gx10._forget_scope(scope)` is the engine endpoint that fans out across all three layers (cold + warm +
  lessons), fail-closed on an empty scope and fail-soft per layer. The Mem0 `/delete_all` route is itself
  fail-closed (an all-empty filter is refused with HTTP 400, never a full wipe). Covered by
  `test_scope_forget.py`.
- **LessonStore seam wired into the engine** (epic #601, S14-4 / AD-10): the `ack.lessons` API is now
  consulted at two sites in the agentic loop, each lazy-imported and scoped to the active project/track
  `mem_scope`. **Read site** — when a task+handover is staged, an advisory lesson `brief([mem_scope])` is
  appended to the handover (a `## Lessons` section) right alongside the existing Memory brief, so a code
  agent inherits prior scoped lessons as context. **Write site** — when a task completes, its feedback is
  reported as a scoped lesson (`report_lesson(mem_scope, feedback, {"task_id", "source": "task_completion"})`),
  gated on a registered provider so that with no backend wired there is **zero** extra work (no file read, no
  call). Both are **fail-soft** (a provider error never breaks a turn) and, with **no provider registered
  (the default), byte-identical to the pre-seam engine** (the brief returns `""` → the handover is unchanged;
  the write is a no-op). The provider itself is epic #602's; this only opens the call sites. Covered by
  `test_lesson_seam_wiring.py`.
- **LessonStore / LessonProvider API — `ack.lessons`** (epic #601, S14-3 / AD-10): the curated, versioned
  public delegation seam for scope-partitioned actionable lessons — the stable surface that **unblocks
  epic #602** (its Distiller registers a provider and goes through this API only, never touching `mem_ns`
  internals). A `runtime_checkable LessonProvider` protocol (`get_lessons` / `report_lesson` / `brief`) +
  `set_provider`/`get_provider`. With **no provider wired the API is a fail-soft no-op** (reads `[]`, writes
  do nothing, a provider error never breaks a turn — lessons are advisory). A scope-priority `brief()` merge
  (delegated, or composed from `get_lessons` over the scopes with dedup + limit). And a **fail-closed,
  redaction-gated `promote()`** (AD-9): a project-private lesson — which may carry paths/secrets — is
  promoted to a broader scope (e.g. curated-global) **only** through a redactor that approves the redacted
  text; a missing/refusing redactor raises. Ships in the wheel (importable from a separate plugin repo);
  the scope is an opaque partition string (the engine passes its `mem_scope`). Covered by `test_lessons.py`.
- **Cross-switch memory re-keying — verified + locked** (epic #601, S14-2): the scope-partitioned memory
  factory the rework called for is satisfied by **per-call** partition derivation over the
  installation-global, overlay-locked connection — a single build-once `MemoryManager`/warm handle resolves
  the **active** project+track partition on every call (`/switch` re-keys via the context; no rebuild, so
  no needless reconnect to the shared store). A registered project always carries a registry-minted
  `mem_ns`, so it can never silently fall back to the base partition; only the `default` project uses the
  legacy base. This invariant is now **locked by tests** (`test_mem_factory_scoping.py`: one handle follows
  A→B→A switches + the track dimension + registered-project-never-base). No production change — a regression
  lock + the recorded design decision.
- **Per-track memory sub-scope** (epic #601, S14-1 / AD-4): a project's memory partition gains the track
  dimension. `ProjectContext.mem_scope()` composes `<mem_ns>::track::<tid>` for a non-`main` active track
  (and falls an unsafe track back to `main`, matching the vault subtree); the bare `mem_ns` — and the empty
  base partition — are returned unchanged, so a single-project / `main`-track install is byte-identical.
  This flows through **both** the cold store (`MemoryManager._ids()` `agent_id`) and the warm tier (the
  session key + the retrieval-cache namespace), so two tracks of one project never see each other's cold
  memories, rolling summary, or retrieval cache. Covered by `test_track_mem_ns.py`.
- **Evidence projector + lifecycle-completeness gate** (epic #601, S13a / AD-7): a public substrate for
  binding dev-process evidence to the vault. `project_evidence(stage, title, body, *, tree_sha,
  content_hash=None, slug=None)` appends a `type: evidence`, stage-tagged doc bound to a `tree_sha` + a
  `content_hash` (sha256 of the body by default) under `<slug>/evidence/`, with a **deterministic**
  filename (`<stage>-<tree_sha[:12]>-<hash[:12]>.md`) — **idempotent** (no timestamp; re-projecting the
  same evidence is a no-op) and **append-only** (it only ever writes new evidence files, never rewrites
  curated bodies), under the vault lock + an index-only reconcile. `lifecycle_completeness(slug, *,
  required_stages, tree_sha)` → `(ready, reasons)` is the fully fail-closed gate the engine DELIVER leg
  runs (not monorepo CI): every required stage must have an evidence doc **and** all must be bound to the
  delivery `tree_sha` (evidence tree_sha == delivery tree_sha). The PRIVATE DELIVER-leg wiring is S13b.
  Covered by `test_evidence_projection.py`.
- **Lifecycle DELIVER-leg gate wired (S13b)** (engine, epic #601 S13b / #632): the S13a primitives are now
  driven by a functioning gate. A new **pure** `engine.lifecycle_projector` maps dev-process **ledger
  transitions** to lifecycle stages — a passed composed-gate run → `tests`, a passed REVIEW leg → `reviews`,
  a `DELIVER` `delivered*` record → `delivery` — and composes `project_evidence` (bound to the delivery
  `tree_sha`, deterministic + idempotent), then runs `lifecycle_completeness`. A `/lifecycle gate --slug
  --tree [--ledger] [--stages]` engine command reads `<repo>/.devloop/ledger.jsonl` as plain data
  (re-verifying the hash chain engine-side — no `scripts/devprocess` import, boundary-clean), projects +
  gates, and reports `READY`/`BLOCKED` **fail-closed** (missing slug/tree_sha, tampered/unreadable ledger, or
  a missing required stage all BLOCK — never a silent pass). The projector is boundary-clean (primitives
  injected; ledger read as data) and engine-resident (not in the wheel). The shipped default `--stages` is
  `delivery` (the only stage the dev-loop producer currently appends to the ledger); `tests`/`reviews` are
  opt-in via `--stages` and become enforceable once the driver's `log` seam feeds the per-unit transitions
  to the ledger (follow-up #830). Covered by `test_lifecycle_projector.py`.
- **i18n of the vault/initiative chrome** (epic #601, S12e-2): the German prose that was hardcoded into
  the self-maintaining vault — the INDEX/LIFECYCLE doc bodies, the reconcile result + "no initiative"
  message, the initiative validation errors, the initiative `meta.md` body, and the mpr task-pipeline
  refusal — now routes through `engine/messages.py` (the engine chrome catalog). **English is the
  source/default** (so the public export carries no hardcoded German), with a **German overlay** selected
  by `gx10.LANGUAGE` (`generation.language` / `GX10_LANGUAGE`). The machine `ironclad:index:auto` /
  `ironclad:related:auto` / `ironclad:lifecycle:auto` HTML markers are **frozen** and never localized.
  Covered by `test_vault_i18n.py` (+ the reconcile/initiative tests ported to the English default).
- **Project-scoped + cross-track reconcile** (epic #601, S12e): `reconcile_active_project()` reconciles
  **every** initiative in the current track (not just the single active one), and `reconcile_all_tracks()`
  is the scheduled **cross-track reconciler** — it sweeps every track of the active project (`main` +
  each `.tracks/<track>/`), **fail-closed per track** (a track that raises is recorded, the others
  continue) and idempotent (delegates to the vault-locked `reconcile_vault`). Wired as
  `/initiative reconcile all`. Covered by `test_cross_track_reconcile.py`.
- **Composable lifecycle stages** (epic #601, S12d): an ordered stage model
  `idea → design → adr → spec → tests → proposals → reviews → delivery` (`LIFECYCLE_STAGES`). A doc
  declares its `stage` in frontmatter, and an initiative's lifecycle is **composed** from its docs'
  stages via `lifecycle_state(docs)` → `present` / `current` / `gaps` / `complete` / `counts` /
  `unknown` (deterministic, no timestamp). The composed state + each node's `stage` are surfaced in
  `GRAPH.json` and summarised in `LIFECYCLE.md`. `can_advance_stage(frm, to, *, allow_regress=False)` is
  the **fail-closed transition guard** (forward-only by default; an empty `frm` admits any valid stage;
  unknown stages refused) — the reusable primitive the `/lifecycle` command (S16) and the DELIVER-leg
  completeness gate (AD-7 / S17) build on. Covered by `test_vault_lifecycle.py`.
- **Typed-edge vault graph — `GRAPH.json` + `LIFECYCLE.md`** (epic #601, S12c): a doc can declare
  **typed frontmatter edges** (`depends_on` / `refines` / `supersedes` / `relates_to` / `implements` /
  `blocks`, each a flat list of doc targets). `reconcile_vault` now also generates, next to `INDEX.md`, a
  deterministic **machine-SSOT `GRAPH.json`** (nodes keyed by full relpath; tags/edges sorted; **no
  timestamp**, so a re-run with unchanged docs is byte-identical) and a human-readable **`LIFECYCLE.md`**
  view. An edge target is resolved (by relpath / stem / bare filename stem) to the target's relpath; an
  unresolvable target is kept verbatim and flagged **`dangling`** (honest, not dropped). The generated
  files are excluded from the doc scan, and the existing `ironclad:index:auto` / `ironclad:related:auto`
  HTML markers stay **frozen** (`LIFECYCLE.md` adds its own `ironclad:lifecycle:auto` managed block). LLM-
  free, idempotent, generated in both reconcile modes. Covered by `test_vault_graph.py`.
- **Per-project + per-track vault-mutation lock** (epic #601, S12b): vault mutation is now serialized
  (Codex S3). `project_registry.Registry.vault_lock(pid, track)` is a per-project + per-track `FileLock`,
  **distinct** from the dev-loop `project_lock` (so a quick reconcile is never mistaken for an in-flight dev
  unit; ADR-0011 AD-2': each track has its own lock). The engine wraps the vault writers (`initiative_new`,
  `reconcile_vault`, and the dev-pipeline macros `_stage_handover`/`_advance_pipeline`) in `_vault_lock()`
  — **reentrant** within a call stack (a nested writer such as `initiative_new → reconcile_vault`, or a
  macro's inner reconcile, does not re-acquire, so there is no self-deadlock), OS-serialized across
  processes/threads, and **fail-soft** (a locking-infra hiccup never blocks a write). Two different tracks
  do not contend. Covered by `test_vault_lock.py`.
- **Per-track vault subtree** (epic #601, S12a): a project's vault gains a first-class **track** dimension
  (ADR-0011 AD-2'). The active track flows from the `ProjectContext` (`_active_track`); the default `main`
  track resolves byte-identically to the pre-track layout, while a non-`main` track is isolated under a
  hidden `.tracks/<track>/` subtree of the project vault (`vault_root` is now track-aware). Track isolation
  needs no exclusion logic — every vault op is slug-scoped or a one-level `*/meta.md` scan and `.tracks`
  carries no `meta.md` — so two tracks keep fully separate initiative trees. Unsafe track ids (traversal /
  separators) fall back to `main` (defence in depth). A single-track install is unchanged. Covered by
  `test_per_track_vault.py`.
- **Strict-locale completeness for generated prompts** (epic #601, S11b-1b): `ack.gate.gate_prompt` gains a
  `strict_locales` flag. By default a *missing* `locales/<lang>.json` overlay for a declared non-source
  language is fine (English fallback — the lenient registration gate, unchanged). With
  `strict_locales=True` a declared non-source language whose overlay is **absent** is a **failure**: a
  prompt that claims to speak a language must actually ship that translation ("declared == delivered"). The
  per-project library invariant `library_items_complete` now applies this to every generated `kind: prompt`
  item (in addition to gating every generated tool), so the self-dogfood / operator completeness check
  covers prompts too. A present overlay is validated under both modes (a malformed translation is always a
  defect). Covered by `test_gate.py` + `test_library_completeness.py`.
- **Prompt-item generation — `generate --kind prompt`** (epic #601, S10c): the paved-road generator
  (`ack.generator`) gains a `--kind {case,prompt}` selector and a `template_root_for(args)` resolver. The
  default, `--kind case`, renders the existing `new-case` tool scaffold **byte-identically** (no `--kind`
  and no `--template` behave exactly as before). `--kind prompt` renders a new `new-prompt` template tree
  into a `kind: prompt` library item — a `SKILL.md` (frontmatter + template body) plus a
  `locales/<lang>.json` overlay — that is **valid on first render** (`ack.gate.gate_prompt`: schema, every
  required variable used in the template, assembles in every declared language) and ready to customise. A
  prompt has no executable `run()` to fill in, so the paved road renders a working fill-me-in template
  rather than a sentinel-marked stub. The engine `/generate --kind prompt` writes into the **active
  project's library** (ctx-resolved `vault/library`), and the prompt path **widens the collision guard** to
  also cover built-in `kind: prompt` capabilities, so a generated prompt can never shadow a seed prompt.
  Covered by `test_generator.py` + `test_generate_command.py`.
- **Generation-completeness gate — `gate_generated`** (epic #601, S11): a registration gate stricter than
  `gate_tool` — a generated item must pass the doctor preflight (CASE + schema + a sibling test) **and** be
  FILLED: it may no longer carry the `ACK-SCAFFOLD-SENTINEL` marker the paved-road generator emits into a
  fresh stub — so a generated item validated with `gate_generated` is **rejected** until it is implemented.
  It can also **execute the behavioural sibling test hermetically** (`run_sibling_test_hermetic` /
  `gate_generated(execute=True)`): a fresh subprocess runs the test in an isolated tmp cwd with a
  **scrubbed** env (no credential/secret/net/memory vars — so it can't reach GitHub / PyPI / the memory
  service) under a **hard timeout**; opt-in, because it runs generated code (the default stays a pure
  no-execution check). Exposed on the curated SDK surface (`ack.sdk.gate_generated` /
  `has_scaffold_sentinel` / `run_sibling_test_hermetic` / `SCAFFOLD_SENTINEL`). The loader **enforces** the
  cheap gate at load — an unfilled scaffold in the active project's library is **dropped** (never offered as
  a tool). A reusable library-wide invariant `library_items_complete(library_root, *, execute=False)` gates
  every generated tool in a per-project library (for the self-dogfood acceptance / an operator); it is not
  auto-run by the dev-process reconciler, which reconciles the repo's GitHub state, not runtime per-project
  libraries.
- **Project-scoped skill/prompt loader** (epic #601, S11): the loader discovers the **active project's
  library** (`vault/library`, ctx-resolved) as the **last, additive** root — after the core built-ins, the
  global plugins dir, and packaged entry points — so a generated per-project capability is offered
  alongside the built-ins. Last + first-kept discovery means it can never displace a built-in; a project
  with no library is **byte-identical** to a single-project install. Closes the generate→discover loop with
  `/generate` (S10). A `/switch` **reloads** the registries (build-then-swap: discovery runs into fresh
  dicts, then the live registries are swapped in — a failed/slow build never empties them) so the new
  project's library is discovered and the previous project's is dropped. An **unfilled scaffold** in the
  library (a tool still carrying the `ACK-SCAFFOLD-SENTINEL` marker) is **dropped at load** — never offered
  as a real tool (S11b-3a: the cheap generation gate enforced on the hot path; the full hermetic test is the
  `library_items_complete` invariant — operator / S17 acceptance, not auto-scheduled). Covered by
  `test_project_scoped_loader.py`.
- **Per-project paved-road generation — `/generate`** (epic #601, S10): the paved-road generator
  (`ack.generator`) gains a `reserved_capabilities` built-in collision guard (and a
  `--reserved-capabilities` CLI flag) — a generated item whose capability would shadow a core built-in is
  **refused** fail-closed before anything is written. The engine `/generate` command renders the template
  tree into the **active project's library** (the ctx-resolved `vault/library`, never `core/skills`), with
  the `core/skills` built-in set injected as the guard — so a `/switch`ed project generates only into its
  own library. Default (no reserved set / no active project) is byte-identical. Covered by
  `test_generator.py` + `test_generate_command.py`.
- **`ack.devprocess.api` — curated public dev-process facade** (epic #601, ADR-0004): a single, versioned
  (`__version__`) public seam exposing the five stable verbs a generated per-project tool delegates to —
  `select_unit`, `stage_handover`, `record_feedback`, `advance`, `deliver`. The dev-process implementation
  substrate stays engine-internal (NOT in the wheel); the facade reaches it through a registered driver
  (`set_driver`/`get_driver`, a `runtime_checkable` `DevProcessDriver` protocol — dependency inversion), so
  it imports from the wheel **alone** and **degrades cleanly**: with no driver registered every verb raises
  a clear `SubstrateUnavailable` instead of an `ImportError`. Shipped in the wheel
  (`packages += "ack.devprocess"`) and covered by the clean-room import-smoke. **In-engine the facade is
  live**: the engine registers a driver at import that wires the two engine-owned verbs (`stage_handover`,
  `advance` → the real impls) and routes the `stage_handover`/`advance_pipeline` tool calls through it;
  `select_unit` / `deliver` (the private dev-loop substrate) and `record_feedback` (the server reconciler
  inbox) report `SubstrateUnavailable` until wired by their owner.
- **Project isolation — `/project` + `/switch`** (epic #601): every engine session now runs in a
  *project* with its own state/vault paths and memory partition. An installation-global **registry**
  (under `GX10_HOME`, atomic write + OS file lock, self-healing) is the SSOT of registered projects and
  the persisted continuity pointer; the engine's active project is **single-active per process** (cached,
  not re-read per request, so a second engine sharing the home can never re-point a running one).
  `/project list|new <id> <path>|active` manages projects and `/switch <project_id>` quiesces and rebinds
  the engine — it saves the leaving conversation under its own root, binds the target's `state_root`/
  `vault_root` + its own `mem_ns` memory partition, and **swaps** the conversation (no cross-project
  bleed). The implicit **`default`** project binds the boot workdir + the legacy/base memory partition, so
  an existing single-project install is **byte-identical**. A switch is **refused** while a dev unit is
  in-flight for either project; locked config keys can never be re-pointed by a project overlay. The
  model's **file tools, `execute_command`, and a launched code-agent now run with the active project's
  root as their working directory** — a `/switch` does **not** `chdir` the process (a global `chdir` under
  the daemons / fan-out threads is unsafe), so the exec cwd is threaded through the active context;
  absolute tool paths are honoured verbatim and the `default` project resolves to the boot workdir
  (byte-identical). The partition reaches the spawned **code agents**: the launched read-only Memory MCP
  carries the active project's `mem_ns` in its env (via `_active_mem_ns`), and the single-writer worker /
  MPR reducer mirror write resolves to the same `mem_ns` through the `MemoryManager._ids()` chokepoint —
  so a code agent reads/writes only its project's memory. New `docs/project-isolation.md`.
- **`dev-process` prompt** (epic #532, DEV 1): a curated, multilingual (EN/DE) starter prompt that
  lays out a disciplined **C0 → C1 → C2 dev loop** (readiness → per-unit → completion) with
  docs-as-code and no-guessing — the GitHub-agnostic dev-process doctrine, dropped in as a
  `kind: prompt` library item (no engine change).
- **`verbatim-scope-audit` prompt** (epic #532, DEV 1): a curated EN/DE prompt that audits a set of
  requirements for completeness — enumerate them `V1..Vn` and map each to a work item before any
  work starts — the DEV-1 prose discipline for full prompt adoption.
- **`dev-loop-runner` prompt** (epic #532, DEV 1): a curated EN/DE prompt that runs **one** unit of
  work through the light DEV-1 loop — **select → work → review → done** over the CLI-agnostic
  per-unit handover — discipline only, stateless and single-unit. The DEV-1 execution primitive as prose.

### Documentation
- **Post-relocation doc drift cleaned** (epic #601 / #828): the starter-prompt `skills/prompts/README.md` table
  now lists all **10** built-ins — adding the public DEV-1 trio (`dev-process`, `verbatim-scope-audit`,
  `dev-loop-runner`, kept public per ADR-0011 D2) it had omitted, matching `status.md`. ADR-0004's evidence
  check notes that `ack.devprocess` now ships only the curated `ack.devprocess.api` facade (substrate relocated
  to private `scripts/devprocess/`, ADR-0011 AD-3); ADR-0011 D1 clarifies that the facade exception (AD-3/D2)
  keeps `packages = ["ack", "ack.lodestar", "ack.devprocess"]` (facade-only); and `status.md`'s ADR-0011
  reference is now a markdown link. Docs-only; no behavior change.
- **Public-grade roadmap** (epic #710, sweep): the generated `docs/roadmap.md` no longer leaks internal
  detail. The dev-process phase dropped the internal "DEV 1/2/3 — three switchable tiers" framing (the
  internal tiers are merged and private; the public face is the versioned dev-process facade + the
  bring-your-own code-agent runner over the extension seam) and is retitled **"Self-maintaining
  dev-process"**; the loop-intelligence and hardening phases dropped private issue numbers — matching the
  already-clean Enterprise/Connector phases. Driven by the open-milestone descriptions (the roadmap source)
  and regenerated; no hand-editing.
- **Doc-text accuracy fixes** (epic #710, sweep): `docs/docs-guide.md` said the roadmap is "Generated from
  open epics" — corrected to **open milestones** (the actual `gen_roadmap.py` rule, matching the rest of the
  doc); the `ack/loop_profile.py` module docstring motivated a "larger retry budget" per task type, but the
  wired ceiling only lets a per-type `retry_budget` be *lowered* (clamped to the hard re-ask ceiling) —
  reworded to match; and the `engine/commands.py` module docstring's local-command list now includes
  `/coders` (it was already in `LOCAL_COMMANDS`). Docstring/doc-only; no behavior change.
- **config-runtime.md now tables the loop-intelligence + provider config blocks** (epic #710, sweep): the
  runtime-config reference documented `search.*` in full but omitted other shipped, operator-steerable core
  blocks. Add a **loop-intelligence** table (`lessons.*` / `quality.*` / `process.*` / `loop_profiles` — all
  opt-in, default OFF, no env override, with their code defaults) and a **provider router** table
  (`providers.*` + the `GX10_PROVIDERS_*` env overrides). `providers.scoring` and `providers.effort_max_tokens`
  are flagged honestly as reserved (the router currently uses fixed built-in values for them). Docs-only.
- **status.md consistency + a re-armed cross-doc guard** (epic #710, sweep): the wiring SSOT had three drifts —
  the TypeScript-client test count was stale (`344`/340 vs the real `360 passing` / `364` total), the **Quality
  Circuit Breaker** row still read `wired + tested` though its own note (and the CHANGELOG honesty entry) says it
  is a delivered seam with no live score-feed yet (now `delivered (seam) + tested`, matching its #602 siblings),
  and the `/lifecycle` slash command was advertised though it does not exist (the `can_advance_stage` primitive is
  consumed only by the DELIVER gate). The TS count drift slipped through because the `doc_reality_audit.py`
  cross-doc guard matched only the README phrasing; it now matches all three (README prose / test-report row /
  status.md row) so any future divergence fails the audit. Covered by two new `test_doc_audit.py` cases.
- **README feature-coverage refresh** (epic #710, sweep): the front-page README now advertises capabilities it
  had drifted behind — **`web_search`** (the trust-gated tool over the vendor-neutral adapter seam, linking
  `docs/web-search.md`) and the **built-in prompt & skill library** (`/prompts` / `/skills` discovery and direct
  `/<prompt-name>` invocation) — teaches the current **`/project new`** workspace command instead of the
  deprecated `/initiative` alias, corrects the latest-release line to **v0.0.20**, and expands the demo
  status-bar caption to list all client-footer indicators (connection, memory, warm, watcher, autopilot,
  coder, web search, throughput). `docs/state-and-initiative.md` notes `/initiative` as a deprecated alias.
  Docs-only; no API change.
- **Memory two-layer, lessons + scope-aware forget** (epic #601, S14-6): `docs/project-isolation.md` gains a
  **Lessons** section (the `ack.lessons` tier — project-private by default, redaction-gated promotion, the
  advisory + fail-soft engine wiring) and documents the **scope-targeted forget** across the cold store, the
  warm tier, and the lesson provider (fail-closed on an empty scope, fail-soft per tier), plus the cold-write
  **scope tagging**. `docs/lesson-api.md` documents the optional `forget(scope)` provider verb and adds a
  **Security** note: a provider must budget the `brief` text and treat lessons as untrusted data, never
  instructions (the brief is injected into a code-agent's handover prompt).

### Fixed
- **Memory-service image build was broken** (`memory-service`, #634): the `Dockerfile` did `COPY app.py`
  only, but `app.py` imports `scope_guard` (#601 S15) + `reflect_policy` (#767) — added with the imports but
  not the COPY — so any rebuild of the current code failed `ModuleNotFoundError` (the deployed image predated
  both, so it never surfaced). The Dockerfile now COPYs every module the service imports (+ `curate`) and
  pip-installs `qdrant-client` (the `/scopes` scroll) + `redis` (the optional reflection backend).
- **Memory-service reflection-trigger race** (`memory-service`, #503 remediation / #767 MEMSVC-1): the central
  threshold-triggered reflection reset its write counter only at the END of a run, so every learning write that
  landed during a (slow) graph-hygiene run re-fired a daemon that immediately bailed on the busy non-blocking
  lock (thread churn) and was then zeroed when the run finished (undercount). The fire decision is now an
  isolated pure policy (`reflect_policy.reflect_decision`, stdlib-only + offline-tested like `scope_guard`): it
  consumes the counter at FIRE time and suppresses a fire while a reflection is already running — so writes
  during a run accumulate toward the next cycle (no undercount) and no bail-thread is spawned (no churn). A
  manual `/reflect` no longer resets the threshold counter (the cadence is now independent of ad-hoc runs).
- **Ink renderer dead-export prune** (`clients/ink`, #503 remediation / #766, INK-R-5/6/7/8 + INK-THEME): removed
  production-unused renderer exports + their now-dead tests (dead-code policy = clean up) — `dispatch.eventPriority`
  (+`EventPriority`), `ScrollBox.visibleRange`/`isVisible`, `Buffers.setCursor`/`clearBack` (+ the cursor-meta
  `FrameMeta` fields they served), `flush.eraseRows`/`flush`, `resize.eraseFrame`, `keys.feedKey`, and the unused
  `WARNING`/`INPUT_TEAL` theme colors. Each was re-verified to have no production caller before removal (the
  hot-path `renderPatches`/`withSync`/`BSU`/`ESU` and the `Ink`-compat-shim `Newline` are kept); stale module
  doc-comments trimmed. TypeScript client tests: 367 -> 359 (8 dead cases dropped).
- **MPR cleanup: dead code, default-ON hint, stemmer, English strings** (#503 remediation, MPR-3 /
  MPR-REG-2/3/4 / MPR-EVAL-2 / MPR-DEAD-1/2/3 / MPR-ENV-2 / TEST-I18N): three genuinely dead surfaces were
  removed — the orphan `mirror_to_memory` (the §6 memory write-back lives only in `synthesis.write_back`,
  the single injected path), the never-called `_voice_from_spec` judge stub, and the unused `_HasContent`
  protocol. The cheap clustering stemmer now trims the **longest** matching suffix, so German forms like
  `Wartbarkeit`/`Wartung`/`wartbar` collapse to one stem (`barkeit` was previously unreachable behind the
  shorter `keit`) — MPR-REG-4. The `/initiative … --type mpr` hint no longer claims "MPR is not active" when
  `mpr.enabled` is simply **unset** — it defaults ON (`MprConfig.enabled=True`), so the hint now fires only
  when MPR is **explicitly** disabled (MPR-ENV-2); a stale "default off" comment was corrected to match. A
  few model-/operator-facing strings in `mpr_research` are now English (the no-active-initiative error, the
  empty-query error, the disabled-gate note, and a test lens label) — MPR-ENV-2/TEST-I18N. The tested-but-
  unwired registry/eval contract surfaces (`SYNTHESIS_BINDING`, `effort_to_template`/`effort_to_max_tokens`,
  the rubric `score`/`weighted_total`, and the §9 `prune_runs` retention utility) are kept and explicitly
  documented as **reserved** contract APIs rather than removed (MPR-REG-2/3, MPR-EVAL-2, MPR-DEAD-3).
- **Memory fallbacks: MCP agent env + search enabled-gate** (#503 remediation, MEM-1/MEM-2): the read-only
  Memory MCP's `memory_from_env` fell back to `GX10_MEMORY_AGENT_ID`, which **nothing sets** — so it silently
  landed on the `"ironclad"` default instead of the configured project namespace; it now falls back to
  `GX10_MEMORY_AGENT`, the **same knob the engine reads** (MEM-1). And `MemoryManager._search` guarded only
  on `self.base` while every write + the other reads gate on `enabled and base` — a `base` + `enabled=false`
  config would still issue `/search`; `_search` now also checks `enabled` (latent today since read sites
  pre-gate, but the contract is now consistent) (MEM-2). Covered by two new tests.
- **Python client/CLI raw-key + claim-race + English UI** (#503 remediation, CLI-2/3/4/5/6): the thin
  client's `_run_handover` was annotated `-> Optional[str]` but returns a `(text, meta)` tuple — corrected to
  `Tuple[Optional[str], Dict]` (CLI-2). `dispatch_pending`'s check-then-add on the shared `claimed` set was
  non-atomic, so an overlapping `/auto` poll + `/work` could double-launch a handover — the claim is now
  serialized under a lock (CLI-3). In the full-screen CLI's POSIX raw-input loop a **bare ESC blocked**
  waiting for two more reads, and a **>3-byte CSI** (PageUp `ESC [ 5 ~`, Home, F-keys) leaked a stray `~`
  into the input; a new `_consume_escape_seq` drains the sequence **non-blockingly** to its final byte (a
  bare ESC is ignored) (CLI-4/CLI-5). And the last German UI strings in the legacy TUI (clipboard / scroll
  hints) are now English (CLI-6). CLI-2/CLI-3 are covered by the existing client-pool tests.
- **Engine routing hygiene + dead code** (#503 remediation, ROUTE-1/ROUTE-2/ROUTE-3/ROUTE-4/DEAD-APPLYCLI):
  the pipeline-advance macro unconditionally spawned **three hardcoded `scripts/*.py`** that don't exist in
  the boundary-clean export (3 fail-soft WARN subprocesses per advance, vessel-specific) — they are now
  driven by `paths.post_advance_hooks` (default empty ⇒ no subprocess; an absent script is skipped), a
  deployment detail kept out of core (ROUTE-1). The dead `_TURN_DID_ADVANCE` guard (only reset, never set or
  read — its comment claimed it blocked a same-turn handover) is removed; the real auto-plan control is
  `AUTOPILOT_AUTOPLAN` (ROUTE-2). `parallel_reason` read `args.effort` for per-item routing but its tool
  schema omitted `effort`, so the model could never set it (pinned to medium) — `effort` is now in the schema
  (ROUTE-3). A plugin/skill tool **named like a built-in** was registered + offered but shadowed by the
  built-in dispatch (undispatchable, silent) — the loader now **rejects it at load** with a warning (ROUTE-4).
  And the uncalled level-4 `_apply_cli` override (the gutted CLI path) is removed, with the precedence
  comments corrected to `code-defaults < file/conf < env` (DEAD-APPLYCLI). Covered by two new tests.
- **Paved-road copier template renders valid output on a raw `copier copy`** (#503 remediation,
  TPL-1/TPL-2): the `new-case` `copier.yml` declared no `date`/`tags_yaml`/`tags_csv` variables, but the
  rendered tree references them (`date: {{date}}`, `tags: {{tags_yaml}}`) — so a raw `copier copy` failed on
  StrictUndefined (the canonical `ack.generator` CLI filled them via `build_context`, masking it). They are
  now declared as computed `when: false` vars mirroring `build_context` (a `tags` question drives
  `tags_csv`/`tags_yaml`; `date` is a prompt the CLI still auto-fills with today) (TPL-1). And
  `non_negotiable` — embedded inside a JSON object in the gap-tracking doc — was a copier `bool`, which
  renders capitalized `True`/`False` (invalid JSON); it is now a computed lowercase `true`/`false` **string**
  (driven by a `non_negotiable_flag` bool question), matching the CLI's lowercase output (TPL-2). Covered by
  two new tests (every template token is declared; `non_negotiable` is a lowercase-json string).
- **POSIX launcher reaches parity with the PowerShell one** (#503 remediation, INSTALL-2/INSTALL-3): the
  `ironclad.sh` EXIT trap only killed an engine **this** invocation started (`STARTED_PID`), so a **reused**
  running engine was never stopped on `/exit` — diverging from `ironclad.ps1`, which stops by listening port
  (#428) and leaving a background `server.py` on the port. `cleanup()` now stops the local engine **by port**
  (whether started or reused), mirroring the `.ps1` (INSTALL-2). And the `.sh` config-reader + engine-env
  omitted `claudeBin`/`fanoutConcurrency`/`workersMaxTokens`/`workersMaxBatchTokens` that the `.ps1`
  forwards — there was no `GX10_CLAUDE_BIN` escape hatch on POSIX; the four keys are now read from the config
  and exported conditionally (via `if/fi`, not `&& export`, so `set -e` can't trip on an absent key)
  (INSTALL-3).
- **Terminal client UI strings are English** (#503 remediation, INK-I18N-1): the recommended client's
  session restore/reset/save messages in `ui/App.tsx` and `state/persist.ts` were hardcoded German
  (`Sitzung zurückgesetzt`, `keine gespeicherte Sitzung`, `… Zeilen wiederhergestellt`, `Sitzung
  gespeichert …`), not behind any language toggle — they are now English (English-only export). (The
  companion INK-I18N-2 — the `runTool.ts` `list_directory` note — was already translated in lockstep with
  the engine in I18N-GX10.)
- **Terminal renderer: drop the unwired blit subsystem; document intentional unwired primitives**
  (#503 remediation, INK-R-2/INK-R-3/INK-R-4): the partial-repaint **blit** path (`render/blit.ts` + the
  `GeomCache` contamination tracking) was built + tested but **never wired** — `renderFrame` full-repaints
  into the back buffer and the front→back cell diff already minimizes the terminal write. It is removed
  (correctness over the micro-optimization), with `blit.ts`/`blit.test.ts` deleted and the contamination
  bookkeeping stripped from `GeomCache` (the absolute-rect cache for hit-testing stays) (INK-R-2). The
  `ScrollBox.onKey` keyboard-scroll and the dispatcher's `onWheel` routing are **generic, tested renderer
  primitives** that the chat client intentionally does not wire — typed keys (incl. `j/k/g/G`) belong to the
  input box (it wires PageUp/PageDown directly), and the wheel scrolls the ScrollBox (consumed before
  dispatch). Both are now documented as intentionally-unwired-here primitives rather than removed or unsafely
  wired (INK-R-3/INK-R-4, operator dead-code policy: keep genuine primitives, decide per item).
- **MPR config/judge correctness** (#503 remediation, MPR-2/MPR-EVAL-1/MPR-ENV-1/MPR-REG-1): a typo'd
  `audit_level` (config/env) was used verbatim, silently dropping the synthesis/perspective artifacts (the
  sovereignty proof) — it is now clamped to the allowlist (`full-per-perspective` on an unknown value)
  (MPR-2). The blind judge's `parse_judgement` coerced **any** non-`'1'` pairwise vote to `'2'`, biasing the
  panel toward slot 2 — it now keeps only a valid `'1'`/`'2'` and drops junk (MPR-EVAL-1). `GX10_MPR_*` env
  overrides were seeded onto the live tree only when the merged config had **no** `mpr` key, so any conf-file
  `mpr.*` made every env read dead (inverting the documented file < env precedence) — env is now seeded once
  per process via a latch, so env beats file while a later runtime `/config set` still persists (MPR-ENV-1).
  And `MprConfig.registry` (the `mpr.registry.*` knobs: roles bounds, effort table, distinctness, panels dir)
  is built but **not read by the resolver** — it is now honestly marked a **reserved** seam (loaded, not yet
  wired) rather than implying the knobs take effect (MPR-REG-1, operator decision). Covered by four new tests.
- **Generator re-run safety + i18n overlay robustness + dead code** (#503 remediation,
  GEN-2/I18N-1/PROMPT-DEAD/PLAYBOOK-DEAD): the paved-road generator recorded a template **baseline for a
  SKIPPED untracked file**, so the next run three-way-merged the user's declined file against a *phantom* base
  — spurious diff3 conflicts in a file the user chose not to adopt; it no longer records a baseline for the
  skipped branch (GEN-2). The `ack.i18n` overlay loader only guarded the **top-level** dict, so `role_lens`
  and `label` chained `.get().get()` and `AttributeError`'d on a malformed nested overlay (e.g.
  `"roles": "x"`) — breaking the never-break-a-run promise; both now isinstance-guard each nested level (via
  `localized()`), matching the already-safe path (I18N-1). Plus dead-code cleanup: unused `field`/`Optional`
  imports in `prompt.py` and the never-read `_SCALAR_FIELDS` constant in `playbook.py` (PROMPT-DEAD/
  PLAYBOOK-DEAD). Covered by two new tests.
- **Registry: PEP-604 schemas, silent-skill diagnostics, locked scan flag** (#503 remediation,
  ACK-1/ACK-2/ACK-3): `derive_tool_schema` gated `Optional`/`Union` on `get_origin(...) is typing.Union`, so a
  modern **PEP-604** `X | None` annotation (whose origin is `types.UnionType`) fell through to a bare-string
  **required** fallback — giving the public SDK a wrong model-facing schema. It now also matches
  `types.UnionType` (ACK-1). The bulk `discover_skills` dropped a module that has a `CASE` dict but an
  empty/typo'd `capability` with **zero diagnostics** (only the single-file `register_skill` raised) — it now
  logs a warning naming the file (ACK-2). And `_ensure_skills_scanned` read/wrote the `_skills_scanned` flag
  **outside** the registry's advertised lock — it now uses double-checked locking under the (re-entrant) lock
  (ACK-3). Covered by two new tests.
- **Warm tier retries after a transient Valkey blip** (#503 remediation, WARM-1): `WarmTier.is_available()`
  dropped a dead client on a failed ping but never cleared the `_tried` connect-once latch — so `_conn()`
  short-circuited on `_tried` and returned `None` forever, making the documented reconnect impossible after
  a transient outage. The failure branch now also resets `_tried=False`, so the next call re-attempts the
  URL. Covered by a new test.
- **Provider routing is robust to an all-disabled pool and bad effort config** (#503 remediation,
  DISP-1/ROUTER-1/SCORING-1): the dispatcher's `active()` keyed on the raw provider pool **including disabled
  entries**, so an all-disabled pool reported active and then routed every item to `no-capable-provider`
  instead of falling back to in-engine fanout — it now gates on the enabled-only `by_id()` (DISP-1). The
  router indexed `EFFORT_RANK[capabilities.max_effort]` with no validation, so a `max_effort` config value
  outside the enum raised `KeyError` out of the dispatch loop — a `field_validator` now normalizes an unknown
  tier to the conservative floor `low` at load (never-raises-into-the-tool-loop, and a typo can never
  over-claim capability) (ROUTER-1). And the
  `providers.scoring` SSOT comment falsely claimed the block was config-overridable while the router applies
  fixed built-ins — the comment now matches the (already honest) `config-runtime.md`: a reserved seam, not
  yet read (SCORING-1). Covered by two new tests.
- **`/coders` works in the legacy full-screen TUI** (#503 remediation, CLI-1): `/coders` is a local command
  but the full-screen Python TUI's command worker (`tui.py`) had no `coders` branch, so it silently did
  nothing there (the thin REPL client and the TypeScript client both implement it). The TUI now mirrors the
  REPL: `/coders` lists the bound coding agents + the fan-out provider lane, and `/coders use <id>|auto`
  pins/clears the runtime agent.
- **Engine surface is English-only regardless of `LANGUAGE`** (#503 remediation, I18N-GX10): a set of
  always-German literals leaked to the model/client irrespective of the configured language — the
  `list_directory` truncation note, the ACK-contract + duplicate `stage_handover` errors returned to the
  model, the handover "context injected" log lines, the idle-workflow marker, the `/config` render labels
  (`source`/`from env`/`set`/`not set`), and the autopilot/autoplan/watcher/log-terminal/rag status +
  warnings (`ON`/`OFF`, the autoplan cost warning). These are now English (the deliberate `language=de`
  CLI output is untouched, and the German *intent-classification keyword* lists stay German — they detect
  German user input, they are not output). The `list_directory` note is **parity-locked** with the
  TypeScript client's local tool runner (`runTool.ts`), so both substrates emit the identical
  `[GX10v3: showing N of M entries …]` the model is tuned to (INK-I18N-2). The existing list-dir tests are
  updated to the English form.
- **`/doctor` is a local command, not a billed model turn** (#503 remediation, DOCTOR): both clients
  advertised `/doctor` as a server command, but the orchestrator's command dispatcher had no `doctor` branch,
  so a forwarded `/doctor` fell through to a billed model turn — while the real gated `GET /doctor`
  (`_doctor_report`) had no in-product caller. `/doctor` is now a **local** command in both clients (and in
  `commands.py` `LOCAL_COMMANDS`): it calls `GET /doctor` and prints the preflight report, exactly mirroring
  `/health`. (The governed `POST /fanout` route flagged alongside it is a **documented, gated, tested**
  external HTTP API — kept as-is, not dead code.)
- **A fresh desktop install boots on defaults (`setup.type=auto`)** (#503 remediation, INSTALL-1): the
  desktop launcher hardcoded `GX10_SETUP_TYPE=local` while the shipped default `base_url` is loopback, and
  the engine rejects `local` + a loopback model (the model is supposed to live on a remote GPU host) — so a
  fresh, unconfigured install aborted at boot. A new **`auto`** setup type derives the topology from
  `base_url` at startup — a **loopback** model ⇒ `server` (fully in-box, in-engine), a **remote** model ⇒
  `local` (the LAN-offload desktop) — and the launchers (`ironclad.ps1`/`ironclad.sh`) now ship `auto`, so a
  default install boots in-engine and pointing `GX10_BASE_URL` at a remote model switches it to `local`,
  with no model host baked into the repo. `sealed` still forces `server`; an explicit `server`/`local` is
  unchanged. Covered by three new tests.
- **Terminal client honors the server per-agent spec and always reports the run signal** (#503 remediation,
  INK-HANDOVER-1/2): the recommended TypeScript client's local handover runner read only `model`/`effort`
  from the `/pending` item and used STATIC client config for `bin`/`cmd_template`/`permission`/`mcp` — so a
  multi-agent registry launched the wrong agent, the gated read-only Memory MCP was dropped under the sealed
  profile, and the `{feedback}`/`{mcp}` template tokens were left literal (INK-HANDOVER-1). It also never
  reported the run's `exit_code`/`stderr` and only POSTed `/feedback` when feedback text existed, so the #455
  budget-failover breaker was unreachable from this client — a budget-exhausted agent retried forever
  (INK-HANDOVER-2). The runner now resolves the full per-agent spec the server ships (a pure, tested
  `resolveLaunch`: an explicit client override > the server item spec > the built-in default), threads
  `mcp`/`mcp_env`, expands `{mcp}` (multi-token) and `{feedback}` like the Python `build_agent_argv`, and
  ALWAYS POSTs the run signal (`exit_code` + a stderr tail) so the breaker trips and fails over. Mirrors
  `client.py` `_run_handover`/`_process_one`. Covered by seven new client tests.
- **Terminal client now opens a Phase-d session (sealed profile usable)** (#503 remediation, INK-SESSION):
  the recommended TypeScript client defined `sessionOpen`/`sessionHeartbeat`/`sessionClose` but never called
  them, so `sessionId` stayed null — under the `sealed` profile every gated route requires `X-Session-Id`,
  so the client 401'd on every turn *and* the 2s status poll, leaving the recommended client unusable
  against a sealed server (only the Python client handled it). A new `establishSession` (a port of
  `client.py` `_establish_session` + `_heartbeat_loop`) runs at startup: GET `/health`, and when
  `security.session` is set it opens a session, keeps it alive on a heartbeat interval (re-opening quietly
  on loss), and closes it on exit. Fail-soft: the `open`/`token` profile or an unreachable server is a
  no-op, never throwing. Covered by four new client tests.
- **Terminal client measures colored text by visible width, not ANSI bytes** (#503 remediation, INK-R-1):
  the renderer's Yoga measure (`layout.ts` `textWidth`/`wrapText`) summed the raw bytes of inline ANSI
  CSI/SGR escapes as display columns, while `paint.ts` consumes those escapes into styled runs and wraps the
  *visible* glyphs — so a colored transcript line was over-measured, given the wrong geometry by Yoga, and
  then re-wrapped/clipped against what paint actually drew (corrupting the colored chat layout the renderer
  exists to draw). `textWidth`/`wrapText` now strip ANSI CSI/SGR escapes (a shared `stripAnsi`, scope
  mirroring paint's `SGR_RE`/`CSI_RE`) before counting columns, so the measure matches the paint. Covered by
  three new client tests.
- **MPR per-run cost/token budget is now enforced and recorded** (#503 remediation, MPR-1): the MPR config
  parsed a `BudgetCfg` (`mpr.budget.*` / `GX10_MPR_MAX_COST_USD` / `_MAX_TOKENS` / `_ON_EXCEED`) but nothing
  ever bound it — the cap was dead, every run was unbounded, and `manifest.budget` stayed `null`. `_engine_deps`
  now builds a `RunBudget` from `cfg.budget`, and the budget is enforced where each resource is actually
  consumed: the **in-engine fanout lane** (the default) clamps the per-lens token budget so the whole panel's
  completion stays within `max_tokens_per_run` (`n × per-lens ≤ cap`; that lane runs on the local host at $0 so
  the cost cap is structurally satisfied there), and the **offload dispatch lane** passes the cost cap to the
  router as a `Budget(usd_cap=…)` so unaffordable candidates are dropped at admission. On both lanes each
  perspective's **real** cost/tokens are charged to the run budget and snapshotted into the run manifest
  (`budget.spent_cost_usd` / `spent_tokens` / `per_provider_spent` + the caps). No budget configured ⇒
  byte-identical (no manifest block, no clamp, unbounded policy). Covered by five new tests.
- **skillgen scaffolds stay valid with quoted/multiline free text** (#503 remediation, GEN-1): the skill
  generator interpolated `description`/`type`/`domain`/`provenance` raw into Python literals + a docstring
  (and into the playbook YAML frontmatter), so a description as mundane as `Wrap text in "quotes"` produced a
  `SyntaxError` module that would not import (then silently swallowed by `discover_skills`) or a broken
  frontmatter block. `render_tool` now serializes every free-text field via `json.dumps` (valid Python/JSON
  literal; the docstring uses the escaped form), and `render_playbook` flattens free-text frontmatter values
  to a single line (the naive `key: scalar` parser only breaks on a newline; the full multi-line description
  is kept in the body). Covered by two new tests (hostile free text → importable module / parseable
  frontmatter).
- **`--validate-tasks` now validates the real task records** (#503 remediation, DOCTOR-VAL): the
  `--validate-tasks` doctor flag was parsed into `DoctorContext.validate_tasks` but no check ever read it —
  the only `validate_task_json` call was on the canonical `EXAMPLE_TASK_JSON`, so per-task `TaskSpec`
  validation never ran (a settable-with-no-effect guardrail). A new `check_task_records_validate` honors the
  flag, validating every stored **base** task record against the live `TaskSpec` and emitting `err` findings
  on drift (capability tasks are Lodestar's `CapabilityTaskSpec` domain and are skipped, not double-flagged;
  no-op when off; never raises). Covered by four new tests.
- **security.profile no longer fails open on a typo** (#503 remediation, SEC-1): an unknown non-empty
  `security.profile` (e.g. `seald`) was silently coerced to the weakest `open` profile, booting a server the
  operator believed was sealed/token-protected. `SecurityPolicy` now keeps an invalid profile verbatim and
  `startup_error()` **refuses to boot** (naming the bad value); unset/empty still defaults to `open`. Covered
  by a new test.
- **Context char-fallback budget reserves sys+tools+thinking** (#503 remediation, BUDGET-1/2/3): on the
  default loopback deployment the live tokenizer is unreachable, so the char-fallback trim is the *sole*
  context guard — but it compared only non-system content against `MAX_CTX_CHARS` (which reserves
  output+RAG+summary, never the system prompt, the tools schema, or `THINKING_RESERVE`), and `total_len`
  ignored tool-call `arguments`, so a dense turn could still push the real request past the model window.
  The char trim now subtracts sys+tools+thinking from the watermark (floored, mirroring the token path),
  counts tool_call name+arguments (via `_message_text`), and an operator-supplied
  `GX10_MAX_CTX_CHARS`/`GX10_TRIM_TARGET_CHARS` is no longer silently overwritten while `TOKEN_BUDGET` is on.
  Covered by three new tests.
- **QualityBreaker snapshot reason no longer self-contradicts** (epic #710, sweep): a trip is latched until
  `reset()`, so after a recovery score zeroes the live streak `QualityBreaker.snapshot().reason` rendered the
  now-zero streak as `quality degraded: 0 consecutive score(s) < 0.50`. It now reports the trip **rule**
  (`tripped on N+ consecutive score(s) < …`) instead of the live streak; the module docstring states the
  latched-until-`reset()` behavior. Mark-only / never-raises unchanged. Covered by a new test.
- **`/config get` with no key is a usage hint, not a crash** (epic #710, sweep): a bare `/config get` (the
  clients `.trim()` the body) fell through to a model turn, and a raw HTTP `config get ` (trailing space)
  raised `IndexError` on `split(None, 2)[2]`. `_dispatch` now matches the bare form too and guards the split,
  printing `usage: /config get <dotted.key>` (mirroring `/config set`). Covered by two new tests.
- **Legacy REPL strips MPR report sentinels** (epic #710, sweep): the ink stream router drops the
  `<<<MPR_REPORT>>>` / `<<<END>>>` machine delimiters, but the Python REPL's `cli.py` `_stream_turn.route`
  did not (its ink port is annotated a "byte-exact port" of it) — so an MPR report rendered the raw sentinels
  in the zero-dependency client. `cli.py` now drops them too, restoring parity and clean MPR output.
- **Installer gates the ink client on Node ≥ 22** (epic #710, sweep): the TypeScript client declares
  `engines.node >= 22`, but npm does not enforce `engines` by default and the installer only checked that
  `node` was *present* — an older Node produced a cryptic `tsc`/npm build error. Both
  `ironclad-install.{sh,ps1}` now read the Node major version and **skip the client build with a clear
  message** on `< 22` (the zero-dependency Python client still works); `install/README.md` states the
  Node ≥ 22 prerequisite.
- **Doctor + deploy verifier surface the warm tier** (epic #710, sweep): `/health` reports the Cold (memory)
  and Warm (Valkey) tiers separately so a silent warm outage can't hide behind `memory: up`, but neither the
  doctor nor the deploy verifier read `warm`. The desktop `ironclad-doctor.{sh,ps1}` now print the memory +
  warm tier state from `/health` (the spark path shows `warm` too), and `deploy/spark/verify-deployment.sh`
  DRIFTs on a configured-but-down warm tier — self-gated on `/health.warm` (`off` = not configured → skip,
  `up` = ok, anything else → drift).
- **Export path-rewrite covers `install/` + `.github/`** (epic #710, sweep): the export lifts `core/`'s
  contents to the published root and rewrites `core/<subdir>/…` path references accordingly, but the rewrite
  group omitted `install` and `.github` — so a `install/ironclad.ps1` reference (e.g. in the CHANGELOG)
  stayed verbatim in the published tree, pointing at a path that ships at `install/ironclad.ps1`. The group is
  now built from a `_PUBLISHED_SUBDIRS` list (incl. `install` / `.github`), guarded by a new test that fails if
  any real published top-level subdir of `core/` is left out, so it can't silently drift again.
- **Command-surface advertising matches the real dispatch** (epic #710, sweep): the advertised command set
  had drifted from what the server actually dispatches. The in-engine `HELP` listed a non-existent `reload`
  command (it fell through to a model turn) and omitted `rag` / `context` / `generate`; `commands.py`
  `SERVER_COMMANDS` + `HELP_TEXT` (the Python REPL/TUI SSOT, also the parity-guard source) omitted
  `rag` / `context` / `tool` / `generate`; and the Ink registry (`commands.ts`) omitted `generate`. Remove the
  `reload` ghost, add the real commands to all three surfaces, so `/help` + autocomplete match reality and the
  `test_ink_client_offers_every_server_command` parity guard now enforces the full set. No behavior change
  (every command already ran via the unknown-`/x`→forward rule).
- **Desktop installer now provisions the warm tier** (epic #710, sweep): the launcher forwards `warmUrl` /
  `GX10_WARM_URL`, but the installer pulled only the `engine` extra — the warm-cache client (`redis`, in the
  separate `memory` extra) was never installed, so the warm tier silently no-opped (`import redis` failed)
  even when a Valkey URL was configured. Both `install/ironclad-install.{sh,ps1}` now install
  `-e ".[engine,memory]"` (matching the Docker image) and accept a `--warm-url` / `-WarmUrl` flag that writes
  `warmUrl` into `.ironclad/config.json`. Warm stays OFF at runtime until a URL is set, so the default is
  unchanged; `install/README.md` documents the flag/env. Also fixes a latent **launcher crash** on the same
  path: `ironclad.sh` forwarded the warm URL with a `${WARM_URL:+GX10_WARM_URL=…}` inline prefix, which bash
  does **not** parse as an env assignment — a configured URL made it try to *run* `GX10_WARM_URL=…` as a
  command and the engine never started; it now exports the var conditionally (preserving an inherited
  `GX10_WARM_URL` when the config has none).
- **`providers.effort_max_tokens` is now honored** (epic #710, sweep): the per-effort output-token cap was
  built into the `ProviderDispatcher` (`self._emt`) but never read — routing always used the hardcoded
  `router.EFFORT_MAX_TOKENS`, so configuring `providers.effort_max_tokens` had no effect. `route_one` now takes
  an optional `effort_max_tokens` table (defaulting to the module values; a malformed table / missing key /
  non-positive value falls back so it stays pure and never raises), and the dispatcher threads `self._emt`
  through both routing call sites (batch dispatch + the server-side `web_search` route). Off-by-default
  (`server` setup) so byte-identical there. Covered by two new `test_router` cases; `config-runtime.md` moves
  the key from "reserved" to the live providers table.
- **TUI now offers `/project` + `/switch`** (epic #601): the Ink client's static command registry
  (`clients/ink/src/commands.ts`) had drifted from its `engine/commands.py` SSOT — it still listed the
  deprecated `/initiative` but was MISSING `/project` and `/switch` (added to the engine in #601 S16), so the
  TUI never suggested them in autocomplete or `/help` even though the server handled them. Added both (ported
  from the SSOT help) **and** a parity guard (`test_commands.py::test_ink_client_offers_every_server_command`)
  that fails if the client registry ever omits a `SERVER_COMMANDS` entry again (ADR-0007 — reconcile the
  derived view). Re-run the installer (or `npm run build` in `clients/ink`) to pick it up.
- **Docs honesty — wiring status of the #602 reflection seams** (post-merge review): `docs/status.md` no longer
  labels the **Strategy Revisor**, **Verifier/Evaluation** and **Quality Circuit Breaker** as "wired" — they are
  **delivered, tested seams/SSOTs with no live consumer on the default engine path** (verify has no engine
  call-site; the strategy seams are invoked only by the MPR plugin, not the core loop; the quality breaker's
  lifecycle is built by config but nothing feeds scores or surfaces trips yet), and `loop_profiles.by_type` is
  **resolved but reserved** (the chat loop uses only the default profile). The honest "seam status" + the
  consumer/deploy-step that lights each one up are now stated per row.
- **ACK reflection-layer hardening** (epic #602, post-merge adversarial review): `classify_emission_failure`
  now has an absolute never-raises backstop (symmetric with `strategy.revise`) — a hostile message/detail
  cannot escape its documented contract; `verify_with_judge` charges the budget **only on a completed call
  that yields a valid verdict** (a transport/parse failure now abstains and charges nothing — no over-charge
  for work that didn't happen); and the engine `_process_hint` limit coercion tolerates a non-finite
  `process.max_hints` (falls back to the default instead of suppressing the hint). All advisory; no happy-path
  change. Covered by `test_failure_class.py` / `test_verify.py` / `test_process.py`.
- **`EngineLessonStore` fail-soft hardening** (epic #602, post-merge adversarial review): `_load` now reads
  fail-soft on a file with **invalid UTF-8 bytes** (`UnicodeDecodeError` is a `ValueError`, not an `OSError`,
  so it previously escaped `report_lesson`/`get_lessons`); `_coerce_category` and `_safe_cap` swallow a hostile
  `__str__`/`__int__` (honouring their never-raises contracts); and a failed `os.replace` no longer orphans the
  temp file. All advisory/fail-soft — no behaviour change on the happy path. Covered by `test_lesson_store.py`.

## [0.0.20] - 2026-06-26

### Added
- **Proper `web_search` tool** (epic #505): the model runs web search natively from Ironclad with a
  strict input contract (`query`, optional `allowDomains`/`blockDomains` — mutually exclusive,
  normalized, wildcard-rejected), a **vendor-neutral adapter seam** (`cli` delegate · a native HTTP
  `brave` adapter on the standard library, **local-only** · a `mock` for tests), structured results
  with a measured duration, a **deterministic `Sources:`** block on every result, and a "web N · Xms"
  status-footer chip (stripped from the chat in every client). Outbound search is **blocked under the
  `sealed` trust profile** unless the operator opts in (`security.web_in_sealed`). Configured via the
  `search.*` block + `GX10_SEARCH_*` env; the API key is name-indirected from the environment, never
  config. `core/` stays standard-library-only (no new dependency). Robust against the model running a
  tool as a shell command (`execute_command` redirects a known tool name — e.g. `web_search "…"` — to
  the tool instead of a shell error), and the current-info steer covers news/headline phrasings. New
  `docs/web-search.md` + `docs/adr/0008-web-search-tool.md`.
- **`/health` reports the Warm tier separately (Cold ⇏ Warm)** (#385): `/health` reported a single
  `memory` field that reflected only **Cold** (Mem0), so a silent **Warm** (Valkey) outage — the tier is
  fail-soft and degrades to a no-op when unreachable — read as a healthy `memory: up` and could regress
  unnoticed. `/health` now also returns `warm` as `up` (reachable) / `down` (configured but unreachable) /
  `off` (not configured), and the Ink footer shows it next to `mem`. Also reconciles the docs that
  conflated Warm and Cold placement: **Cold (Mem0) is model-host-pinned** (GPU/LLM-coupled); the **Warm
  cache (Valkey) follows the orchestrator** — loopback is the ideal, a LAN hop to the model host is
  acceptable (Decision D-Valkey). `test_server_split.py` (+1), `clients/ink` (+warm footer assertions).
  (Core — `server.py` `/health`; client — Ink footer + status poller; docs — `setup-types.md`,
  `docker-compose.yml`.)

## [0.0.19] - 2026-06-25

### Changed
- **Node toolchain validated end-to-end (Node 22 → 24)** (#448, epic #440): the `clients/ink` CI now runs a
  **Node matrix `[22, 24]`** (`node-client.yml`), so CI validates BOTH the `engines` minimum (Node 22, npm 10)
  AND the version a desktop actually ships (Node 24, npm 11) — closing the gap where CI never tested the
  toolchain the operator builds/installs with. Each matrix leg uses its bundled npm, so the npm 10 ↔ 11
  resolution skew is covered; the `package.json` declares the canonical `packageManager` (`npm@11.17.0`) for
  corepack users (verified `npm ci` is in sync — no lockfile drift). A `process_doctor` invariant
  `node-version-matrix` keeps the matrix from regressing. The public `clean-room.yml` pre-publish job stays
  single-Node on purpose (it is a branch-protection required check; matrixing it would rename the check and
  needs a coordinated, operator-gated protection update). `test_process_doctor.py` (+2).

### Added
- **Read-only Memory MCP for external coding CLIs** (#480, epic #440 Phase 6 / FORK-G D2): a dependency-free
  **stdio MCP server** (`engine/memory_mcp.py`, JSON-RPC 2.0) that exposes the project memory to an
  MCP-capable code CLI (Codex/Claude) as **read-only** tools — `memory_search` (vector) and
  `memory_deep_query` (graph), no write (write-back deferred). The CLI spawns it as a subprocess; the
  code-agent registry injects the per-CLI MCP config via a new `{mcp}` multi-token placeholder in the
  `cmd_template` (filled from the agent's `mcp_template`). The injection is **gated server-side on the
  `sealed` trust profile** (operator) AND a configured memory service — under `open`/`token` the launch is
  byte-identical to before. **Secret-free**: the memory connection travels in the spawned process's env
  (`GX10_MEMORY_URL`/`GX10_MCP_MEMORY_NS`), never on the MCP wire; the read is scoped to the **project
  namespace** (the same memory the orchestrator + the #458 handover brief use), not the code-agent's id.
  Fully fail-soft (a memory hiccup returns a tool result, never crashes the server). `test_memory_mcp.py`
  (new, 10), `test_client_pool.py` (+1), `test_server_split.py` (+3). (Core — `memory_mcp.py` +
  `commands.build_agent_argv` `{mcp}` + `gx10`/`server` gate + `client` env; the per-CLI `mcp_template` is a
  private `conf/` detail.)
- **Onboard-but-disabled code agents** (#460, epic #440 Phase 6): a code agent can now be **registered
  while `enabled: false`** — onboarded but **inert** until activated. A disabled agent is excluded from the
  enabled-only launch/schema surface (`names`/`has`/`resolve`/`by_agent`), so it is never offered in the
  handover schema, never boot-probed, never launchable, and **never a budget-failover peer** (even if its id
  is listed in a `code_agents.classes` set — `resolve()` returns None, so it is skipped); `validate_loud`
  still checks it is well-formed. New `CodeAgentRegistry.all_ids()`/`spec_of()` expose ALL registered agents
  (including disabled) for **operator visibility**: `GET /coders` (and all four clients) now show an
  onboarded-but-disabled agent as `enabled:false` / `(onboarded · disabled)`, so the operator can see a
  registered agent that is pending activation. This is the onboarding seam for a new backend whose
  exhausted-signal is calibrated from one real run before it is enabled. `test_code_agent_registry.py` (+3),
  `test_server_split.py` (+1). (Core — `providers.py`/`server.py` + the four clients.)
- **First-class web search + current-info routing + shell guardrail** (#459, epic #440 Phase 6 / §4 /
  FORK-H — fixes the verified scaling-break #447): the orchestrator gains a real `web_search` tool so the
  model never has to improvise a shell web fetch for current information. Three parts: **(1)** a `web_search`
  tool, offered only when a **web-capable provider is configured**, that runs the search **server-side
  through the provider lane** (`ProviderDispatcher.web_search` routes a `needs_web`/PUBLIC request to the
  web provider and runs it via the **captured** CLI runner — structurally immune to the console write that
  corrupted the renderer); **(2)** a conservative EN+DE **current-info intent classifier** that proactively
  steers "latest / today / aktuelle Lage" requests to `web_search`; **(3)** a **fail-closed shell
  guardrail** on `execute_command` — a remote/web fetch (`Invoke-WebRequest`, `curl`, …) or an
  unbounded/progress-emitting process (sleep loops, `-Wait` follows, watchers, `ping -t`) is **refused
  before it runs** (and a web fetch is redirected to `web_search`), plus the PowerShell invocation is
  hardened with `$ProgressPreference='SilentlyContinue'`. The guard fires **server-side at the top of
  `run_tool`, before the local-tool bridge**, so it covers every client (the thin client AND the Ink
  client, which also gets the PowerShell hardening at its own execution site). The deny-list anchors bare
  `curl`/`wget` to a command position (a filename/search string merely *containing* the token isn't
  blocked), and the intent classifier avoids bare "current" (everywhere in coding context). The web CLI
  itself (e.g. Codex `--search`) is a private `conf/` deployment detail; core ships the capability-gated
  mechanism. The guard is **mirrored into the Ink client** (`shellGuard` in `runTool.ts`) so the local
  `/sh` escape hatch — which never reaches the server — is also covered, and the web provider must be an
  **enabled external CLI** (an in-engine/disabled web spec is never offered or routed). `test_dispatch.py`
  (+7), `test_websearch.py` (+46), `clients/ink` `runTool.test.ts` (+4). (Core — `dispatch.py`/`gx10.py`;
  client — `clients/ink/src/tools/runTool.ts`.)
- **Token-budgeted Memory brief in the handover** (#458, epic #440 Phase 6 / FORK-G D1): the plain
  `type: title` `get_context` that was appended to a staged handover is replaced by a richer, token-budgeted
  **Memory brief** for every code agent — so the external coding CLI starts a task with the same memory the
  orchestrator has. The brief composes, in priority order and trimmed to stay within a token budget
  (`context.memory_brief_tokens`, default 1200; counted with the best-available tokenizer): (1) the shared
  **warm rolling summary** (the main loop's common ground), (2) **body-keyed** vector hits — the handover
  BODY is a far richer retrieval query than `type: title`, (3) optional **relational** (graph) hits, deduped
  against the vector hits. Fully **fail-soft**: any memory hiccup (or nothing relevant) just stages the
  handover without a brief, and a vector-search error still keeps a warm summary already in hand. `get_context`
  remains for plain callers. `test_memory.py` (+7). (Core — `memory.py` `MemoryManager.brief` + the
  `stage_handover` injection in `gx10.py`.) The read-only Memory **MCP** for external CLIs (D2) is tracked
  separately (#480, stdio transport).
- **Distinct-reviewer routing (anti-affinity)** (#457, epic #440 Phase 5): the pure provider router can be
  told to **avoid the agent that produced the artifact under review**, so a review-of-a-review is never
  routed back to its own author while an equal peer is available. A new `RouteRequest.excluded_provider_ids`
  (caller-passed — `route_one` stays pure and snapshot-testable) drops those providers from the candidate
  set after the capability filter, before load/spill/budget (so the producer can't slip back in via a
  fallback). It is **SOFT**: if excluding the producer would leave no capable agent, the route is not
  declined — the producer is kept and the decision records the waive. `RouteDecision.distinct_reviewer`
  carries the provenance: `"applied"` (an excluded producer was dropped and an equal peer chosen),
  `"waived"` (the producer was the only capable agent), or `None` (no exclusion requested). **HARD axes
  outrank it**: a SENSITIVE/local-only request whose only local provider is the excluded producer stays
  local (waived) rather than leaking to an external "distinct" peer. `test_router.py` (+5). (Pure router
  seam — `router.py`; a private CI invariant `review-distinct-reviewer` asserts the seam stays intact.)
- **Task-class-scoped budget failover** (#456, epic #440 Phase 5): the #455 failover now stays within the
  agents that are *capable of the task's class*, so a budget-exhausted Opus on a **security** or
  **architecture** task never silently falls to a cheaper-but-weaker peer. The task class is derived
  **deterministically from `task_json.type`** (`gx10._task_class`, FORK-D — no model self-report is
  trusted): `security`/`security-audit` → `security`, `architecture` → `architecture`, `verification`
  → `analysis`, everything else → `coding`. A new `code_agents.classes` capability map
  (`security: [OPUS]`, `architecture: [OPUS]`, `coding: [OPUS, SONNET]`, `analysis: [SONNET]`) names which
  registry agents may serve each class; `_effective_code_agent(staged, task_class=…)` restricts the
  cheapest-non-tripped-peer search to that set. The **staged (orchestrator-chosen) agent stays
  authoritative** and an operator pin still wins — the class only *scopes the failover peers*. Fail-open
  by design: an unknown/unmapped class (or no class) imposes no restriction, byte-identical to #455; if
  every capable agent is tripped the chosen one is kept (fail-closed, never an out-of-class agent).
  `test_server_split.py` (+12). (Core seam — `gx10.py`/`server.py`; the conf `classes` map refines the
  per-deployment roster privately.)
- **Budget-exhausted classifier + circuit-breaker + equal-peer failover** (#455, epic #440 Phase 4):
  turns an out-of-budget agent's silent infinite-retry into a clean failover. The client now reports the
  raw run signal (exit code + a bounded stderr tail) to the server, which **classifies** it
  (`providers.classify_agent_result`, FORK-C=C: layered JSON-event → stderr regex → exit code, patterns
  from conf) into `ok-feedback` / `task-failed` / `agent-unavailable`. A run that produced FEEDBACK is
  always `ok-feedback` — the feedback (the agent's task result) is never pattern-matched (so a coding
  answer that mentions "rate limit"/"quota" can't false-trip); only the RAW stderr is scanned, only when
  there is no feedback. **Conservative**: only an explicit exhausted match is `agent-unavailable` — an
  unknown failure is `task-failed`, never a wasteful failover. On `agent-unavailable` the server trips a **process-lifetime circuit-breaker**, and
  `_effective_code_agent` fails over to the **cheapest non-tripped capable peer** (USD soft ordering;
  all tripped → keep the chosen one, fail-closed). `GET /coders` shows breaker-tripped agents; pinning an
  agent (`/coders use <id>`) clears its breaker (recovery). Generic, public-safe exhausted patterns ship
  in core; a deployment refines per-agent in `conf/` — an agent's exact signal is calibrated from one
  consented run (e.g. Kimi at #460). `test_code_agent_registry.py` (+9), `test_server_split.py` (+4).
  (Core seam — `providers.py`/`gx10.py`/`server.py`/`client.py`; conf patterns private.)
- **Runtime coding-agent switching** (#454, epic #440 Phase 4): the operator can pin which coding
  agent handles ALL handovers at runtime with `/coders use <id>` (and `/coders use auto` to clear) —
  the runtime "switch" between equal-strength agents. A new `code_agents.pinned` runtime config, a
  guarded `POST /coders` (validates the agent against the registry, fail-closed on unknown), and a
  single `gx10._effective_code_agent(staged)` seam that **overrides the orchestrator's task-chosen
  (staged) agent** at every execution/reconciliation point (`/pending` spec resolution, the reconciler
  feedback match — with a staged fallback if the pin changes mid-handover, and the autopilot launch).
  No pin ⇒ the staged agent (task-appropriate — the orchestrator already routes "Opus for
  security/architecture"); cost-based auto-routing among task-equal peers is deferred to the Phase 5
  `task_class` work. `GET /coders` + the `/coders` view (all four clients) show the active pin.
  `test_server_split.py` (+5). (Core seam — `gx10.py`/`server.py` + the four clients.)
- **Live "which coder is being called" indicator** (#453, epic #440 Phase 4): the fan-out routing
  provenance (`provider_id`/`route_reason`/`spilled`) is now surfaced as a typed, backward-compatible
  `[agent]` control frame — the same line-protocol pattern as `[perf]`. The orchestrator emits one
  frame per distinct routed provider (`_emit_agent_frames`, fail-soft); every client parses it out of
  the chat stream into the status footer (`coder <id> · <reason>`): the TypeScript client
  (`stream/route.ts` + `Footer`), the Rich full-screen client, the Textual client, and the REPL. The
  parser is a byte-exact port across the Python (`cli.py`) and TypeScript renderers. `test_server_split.py`
  (+2), `route.test.ts`/`components.test.tsx` (+1). (Core seam — `gx10.py` + the four clients.)
- **`/coders` — which coding agents are bound + active** (#452, epic #440 Phase 4): a new guarded
  `GET /coders` and a `/coders` client command (REPL, full-screen, and the TypeScript client) answer
  "which coding agents are actually bound right now". It surfaces the config-driven code-agent registry
  with each agent's prompt-free **boot-probe liveness** (bin resolved = bound) alongside the fan-out
  provider lane via a new `ProviderDispatcher.snapshot()` (per-provider reachability + last routing
  reason + running budget). `/health` carries a compact `coders: {bound,total}` for the 2s poller.
  `GET /coders` is gated like `/tasks`/`/doctor`. `test_dispatch.py` (+4), `test_server_split.py` (+4),
  `classify.test.ts` (+1). (Core seam — `dispatch.py`/`server.py`/`commands.py`/`client.py`/`cli.py` +
  `clients/ink`.)
- **Per-agent boot probe** (#451, epic #440 Phase 3): the server checked a single `which(CLAUDE_BIN)` to
  decide whether a local code-agent was available. It now probes EACH enabled code-agent (prompt-free
  path resolution, `providers.probe_code_agents`) and is cli-available iff AT LEAST ONE resolves —
  fail-closed only when ZERO do. Each agent's binary resolves via `PATH` (a stable shim) else the spec's
  private-layer `bin_glob` newest-by-mtime (FORK-A3: the hashed AppData launcher path rots on update);
  env vars/`~` in `bin_glob` are expanded, and the concrete path stays in `conf/` (never a literal in
  `core/`). Boot logs each agent's resolution. `test_code_agent_registry.py` (+7). (Core seam —
  `providers.py`/`server.py`; private config — `conf/`.)
- **Config-driven code-agent registry — the multi-CLI spine** (#449, epic #440 Phase 3): the handover
  code-agent identity was fused to Claude across six OPUS/SONNET allowlists, a `client._MODEL_BY_AGENT`
  table and a legacy KIMI→SONNET normalization. Agents now live in ONE config-driven, always-on surface —
  `config.code_agents.pool`, a `providers.CodeAgentRegistry` keyed by `agent_id` — SEPARATE from the
  fan-out `providers.pool` (so it is independent of `providers.enabled`, which is on in local-mode).
  Ironclad ships **OPUS**/**SONNET** as **overridable** defaults; `conf/` adds CODEX (KIMI at #460). The
  `agent_id` is a letters-only filename token (it must round-trip BOTH `_HO_AGENT_RE` and `_FB_RE`); an
  **unknown agent fails closed** everywhere (the two `stage_handover`/`advance_pipeline` guards, the
  autopilot reconciler, the server pull) instead of silently defaulting. The handover schema `agent` enum
  is generated from the LIVE registry, so a conf-added agent is offerable. The server now resolves the
  FULL agent spec (`bin`/`cmd_template`/`model`/`effort`/`permission`) from the registry into the
  `/pending` item and the client is a thin renderer (no client-side registry); the handover-frontmatter
  `to:`/`effort:` still override. Both launch paths share one `build_agent_argv` (moved to stdlib
  `commands.py` so the zero-dependency client never pulls pydantic). `test_code_agent_registry.py` (+32).
  (Core seam — `providers.py`/`gx10.py`/`server.py`/`client.py`/`commands.py`; private config — `conf/`.)
- **Deterministic code-agent result capture (hybrid feedback)** (#443, epic #440 Phase 2): the handover
  runner read only the agent-written `…-feedback.md`, so an agent that finished its work but forgot to
  write that file produced a silent no-feedback → retry. `_build_agent_argv` now accepts an optional
  `{feedback}` token (a result-capture path) and `_run_handover` threads it through; an agent whose
  template uses it — e.g. Codex `-o {feedback}` (now in the conf Codex entry) — writes its FINAL message
  there, and the runner falls back to that captured message when the in-prompt feedback file is missing
  (FORK-A2 = C, hybrid: file is primary, capture is the fallback). Claude's default template omits
  `{feedback}` and is unchanged. `test_client_pool.py` (+4). (Core seam — `client.py`; private conf.)
- **Codex is a first-class code-agent backend on the template-driven client lane** (#442, epic #440 Phase 2):
  a `codex` provider entry in the private `conf/` pool proves the existing template-driven handover lane
  runs Codex with ZERO core change. The Codex `cmd_template` (`codex exec -m {model} -s workspace-write -c
  'approval_policy="never"' --skip-git-repo-check {prompt}`) **drops `{effort}`/`{permission}`** — `codex
  exec` rejects `--effort`/`--permission-mode`/`-a` (verified live, §C0R-8) — and `_build_agent_argv` leaks
  none of the Claude-only flags/values. `bin: "codex"` is the logical name; the per-provider boot probe
  (#451) resolves it glob-newest from `%LOCALAPPDATA%\OpenAI\Codex\bin` (the hashed path rots). Output
  capture (`-o`/`--json`) is #443; the live workspace-write run is operator-verified later.
  `test_client_pool.py` (+1). (Private config — `conf/`; the BYO-code-agent guide
  `code-agents.md` + `status.md` gain a verified Codex example + the `{feedback}` placeholder, #444.)
- **Multi-line paste collapses to a `[Pasted #N +L lines]` placeholder in the TUI input** (#438): pasting
  more than one line into the chat input now shows a compact placeholder (like Claude Code) instead of the
  raw lines, and expands back to the full text when the turn is submitted — so a large paste no longer
  floods the input line. Re-introduces the model the retired Python TUI had (`tui.py`), ported to the
  TypeScript client: bracketed **and right-click** pastes are flagged (`key.paste`) at the mount layer, a
  pure `pasteStore` module owns the compress/expand/Backspace logic, and `App.tsx` keeps the per-turn
  block store. Hardened after an adversarial review: the buffer holds an **out-of-band sentinel** (not the
  visible grammar), so typing or single-line-pasting the literal `[Pasted #N +L lines]` is never expanded
  or over-deleted; deleting a collapsed paste **reclaims** its stored block; the sentinel delimiters are
  stripped from incoming paste content so a paste can't forge one; and LF/CRLF/lone-CR are all treated as
  line breaks. Single-line pastes and typed input are unaffected. `node:test` (+13).

## [0.0.18] - 2026-06-24

### Fixed
- **Token-accurate context budgeting** (#371, epic #366 P1 1/3): the context budget was char-based
  (`CHARS_PER_TOKEN = 4`) against a hard 32 768-**token** wall. Real agent content (code/JSON/CJK) is
  ~2–2.6 chars/token, so the working set silently exceeded the window and vLLM rejected the request
  before generating (`HTTP 400 "maximum context length is 32768"`, `0 gen · 0 tok`). The trim now
  counts **real tokens** via the served model's tokenizer (the vLLM `/tokenize` endpoint — no bundled
  tokenizer dependency) and the RAG block is budgeted in real tokens; the calibrated chars/token
  fallback (default **2.6**) is used only when the endpoint is unreachable (conservative — it trims
  early rather than overflowing). Fail-soft: a tokenizer outage never makes the engine 400. Also fixes
  the false `_derive_ctx_budget` docstring ("never overflows it" held only at 4 c/t). See ADR-0003.
- **Pre-flight overflow guard + emergency single-turn trim** (#372, epic #366 P1 2/3): `_make_completion`
  now checks, before every vLLM call, that the prompt + the reserves it must leave free — output
  (`max_tokens`) + the **tools schema** vLLM serializes into the prompt + the **conditional thinking
  budget** (only when `think=True`) — fit the model window. If not, it does ONE emergency trim of the
  oldest **whole** rounds (atomic `assistant.tool_calls` + their `tool` responses, else a different 400);
  if it still can't fit (an irreducible single oversized turn) it raises a clear **`ContextOverflowError`**
  (prompt/output/window sizes) instead of a raw vLLM 400. Evicted rounds are archived losslessly to cold.
  Fail-soft (skipped when token budgeting is off). See ADR-0003.
- **Bounded summarizer input** (#373, epic #366 P1 3/3): `_summarize` capped only its OUTPUT
  (`SUMMARY_MAX_TOKENS`), never its INPUT — a large evicted transcript was fed whole, so the summarizer
  call itself could hit the model window and vLLM would silently truncate it (a lossy rolling summary,
  state loss over long sessions). The input is now bounded token-based, **tail-first**
  (`input_budget = min(4096, max_model_len // 4)`); the **full raw transcript is still archived losslessly
  to cold first**, then only the most-recent (tail) rounds within budget are summarized (a warning is
  logged on truncation). Completes the P1 trio. See ADR-0003.
- **`ironclad` `/exit` now stops the local background engine** (#428): the launcher
  (`install/ironclad.ps1`) started or reused a local `server.py` and only stopped it on exit
  *when this session had started it* (`$started`), so a **reused or orphaned engine lingered on the
  port** after `/exit` (the CLI quit but `server.py` kept serving). The teardown now stops the local
  engine by its listening port regardless — the engine is ephemeral per session (single-tenant by
  design; the `spark` thin-client path is unaffected — it has no local engine).

### Added
- **`GX10_CHARS_PER_TOKEN`** / **`GX10_TOKENIZE`** env knobs (#371): tune the calibrated fallback
  ratio; `GX10_TOKENIZE` is `auto` (probe a real remote/LAN host), `1` (force — server-mode loopback),
  or `0` (pure char fallback).
- **`GX10_THINKING_RESERVE`** env knob (#372, default 4000): output headroom reserved for the thinking
  budget, applied only when thinking is on for the call.
- **Live `max_model_len` discovery** (#377, epic #366 P2): the engine reads the served model window
  from `GET /v1/models` at boot and adopts it (re-deriving the char-fallback watermarks), so the token
  budget can't drift if the Spark is relaunched with a different `--max-model-len`. Fail-soft (keeps the
  configured `MAX_MODEL_LEN` on any error); only a real remote/LAN host (the offline suite stays
  hermetic); `GX10_DISCOVER_WINDOW=0` disables.

### Documentation
- **Windows conhost copy/scaling limitation** (#382, epic #366 P3): documented the legacy Windows
  console (`conhost.exe`) right-click-copy / scaling limitation in the `clients/ink` docs — the renderer
  owns selection (alt-buffer + SGR mouse tracking), so on conhost a right-click copy is captured by the
  app; *workaround:* Windows Terminal + Shift-drag. Terminal limitation, not a renderer bug; shares the
  alt-buffer + mouse-tracking class with #256 (where the client-side fix is tracked). Docs only.
- **Output reserve is tunable** (#379, epic #366 P2): documented that the output (generation) token
  reserve — `generation.max_tokens` / **`GX10_MAX_TOKENS`**, default **8192** — is the permanent output
  budget subtracted from the model window. The default stays 8192 (raising it from 4096 in PERF-10 fixed
  long-handover truncation; post-#371 it is reserved token-accurately). Raise it for longer single
  outputs, lower it for more context headroom. Addresses C-5.

## [0.0.17] - 2026-06-24

### Fixed
- **Publish workflow permissions** (#411): the publish job set `permissions: id-token: write` explicitly,
  which drops the default `contents: read`, so `actions/checkout` failed on a private repo
  (*"repository not found"*). Grant `contents: read` back (a no-op on the public repo; required for a
  private Test-PyPI proof repo).
- **Post-publish-smoke install index** (#413): the post-publish smoke `pip install`ed `ironclad-ai==<ver>`
  with no index, so it queried production PyPI even for a Test-PyPI release and could not find the version.
  Install from the SAME index the release published to (derive the simple index from `PYPI_REPOSITORY_URL`
  + add production as an `--extra-index-url` for dependencies); unset keeps production-default.

### Added
- **Repo-scoped publish index** (#397, epic #348 S14c): the publish workflow's `repository-url` is now
  repo-scoped via the `PYPI_REPOSITORY_URL` variable (default: production PyPI). This lets a separate
  Test-PyPI proof repo (`ironclad-testpypi`, with its own Test-PyPI Trusted Publisher + the variable set to
  `https://test.pypi.org/legacy/`) publish the SAME generated workflow to Test-PyPI — so the release chain
  is proven on Test-PyPI first, with both push- and index-isolation, before any production cut. Production
  `ironclad` is unaffected (the variable is unset → production PyPI).
- **Deploy/spark consistency** (#216, epic #210): the deployed Spark topology is a derived view of the
  released artifact. CI-runnable (no SSH): `scripts/ci/check_deploy_consistency.py` asserts every literal
  `setup.type` value in the deploy scripts is one the engine accepts (`_VALID_SETUP_TYPES`) and that no
  script references a missing `deploy/…` script — caught + fixed a terminology drift (two scripts said
  `setup.type=desktop`, an install-type name the engine rejects). Operator (SSH + local): `deploy/spark/
  verify-deployment.sh [--reconcile]` checks the live deployment — **topology- AND location-aware**: the
  model is probed on the Spark, and the orchestrator is verified **where it actually runs** — on the
  **desktop** (`localhost:8100`) for `setup.type=local`, on the Spark for `server` — asserting `/health`
  `ok`, `memory=up`, and (local) that `base_url` points at the Spark over the LAN. Verified live: the
  current `setup.type=local` deployment is consistent (Spark model up; desktop orchestrator up, memory up,
  wired to the Spark). `test_deploy_consistency.py` (5). Per ADR-0007.
- **Label taxonomy + anchor-hygiene invariants** (#215, epic #210): three warn-tier process-doctor
  checks surfacing governance drift for triage — `labels-match-taxonomy` (the repo's defined labels
  equal the bootstrap taxonomy: a rogue or a deleted label), `issue-has-type-label` (every open issue
  carries exactly one `type/*`), and `merged-pr-anchored` (every merged PR links an issue, release/
  export PRs excluded). `test_process_doctor.py` (+5). Per ADR-0007.
- **Required-status-checks SSOT + audit** (#214, epic #210): the branch-protection required checks were
  invisible to version control, and the #196 `secret-scan` job was **not** required — a secret leak
  wouldn't block a merge. Added `.github/required-status-checks.yml` (SSOT) + `check_required_checks.py`
  (asserts every required name maps to a real workflow job, matrix names expanded — CI-gated, no API) +
  process-doctor `required-checks-live` (asserts the live protection equals the SSOT; fail-soft without
  an admin read). One-shot: added `secret-scan` to the live ironclad required checks. `test_required_checks.py`
  (5) + `test_process_doctor.py` (+2). Per ADR-0007.
- **Plugin-mirror parity** (#213, epic #210): the plugin round-trip (`mirror-from-plugin.yml`) lacked
  the back-link + liveness invariants the upstream one got, and labelled `triaged` BEFORE creating the
  dev mirror (a partial failure stranded a framework-gap report). Hardened the workflow to
  create-before-triage (parity with #194) and added two scheduled process-doctor warn checks —
  `plugin-triaged-has-mirror` (a triaged plugin issue with no dev mirror) + `plugin-mirror-live` (the
  intake's last run failed). Both fail-soft when the operator-gated plugin repo is unreachable.
  `test_process_doctor.py` (+3 = 29). Per ADR-0007.
- **Release tag ↔ CHANGELOG ↔ pyproject coupling** (#212, epic #210): `release_preflight` (#198) only
  guards PRE-publish; a post-release metadata mutation (a deleted tag, a reverted CHANGELOG section)
  is seen by no path-gated CI. Two scheduled process-doctor checks close it: `release-tag-has-changelog`
  (every published ironclad release tag has a `## [X.Y.Z]` section — fail) and `changelog-has-release-tag`
  (every released section has a tag — warn, excluding the cut-but-unreleased current version). Healed the
  drift it found: backfilled the missing `[0.0.1]` + `[0.0.2]` sections (early pre-releases predating the
  CHANGELOG). `test_process_doctor.py` (+4). Per ADR-0007.
- **Test counts are generated, not hand-maintained** (#211, epic #210): the Python test counts in
  README + `docs/test-report.md` are a derived view of the suite (they drifted every PR). New
  `scripts/ci/gen_test_counts.py` runs the offline suite and `--check` fails on any drift / `--write`
  regenerates; it also asserts the per-area breakdown is a true partition (the area rows sum to the
  total) and that every offline skip is a live-smoke test (so the offline/live split is honest). A CI
  `test-counts` job enforces it. `test_gen_test_counts.py` (9). Per ADR-0007.

### Fixed
- **ironclad-doctor reports the running engine version, not just the install stamp** (#255): on a
  desktop install the doctor printed `local engine version` straight from the on-disk `VERSION` file
  (the *installed* stamp), so an installed-vs-running drift was invisible — `ironclad-install` re-stamps
  and re-copies `core/` but does not restart the live engine (the `ironclad` launcher does, on next
  start), and `orchestrator_version` is frozen at boot. The doctor now also reads the running engine's
  `/health.orchestrator_version` and prints `installed engine version=X` plus the running version,
  warning `running 'Y' != installed 'X' — run 'ironclad' to restart` on a mismatch.
- **Example plugin passes its own documented `ack.sdk.gate`** (#260): `examples/example-plugin` shipped
  no sibling test, so the validate step it documents (`gate("…/reverse.py")`) failed out of the box —
  the gate hard-requires `<package>/tests/test_<stem>.py` (`ack/gate.py`). It now ships
  `ironclad_example_plugin/tests/test_reverse.py`; the README shows the gate as a passing `assert`, and
  `test_example_plugin.py` asserts the gate passes (regression guard). Surfaced via the plugin round-trip.

## [0.0.15] - 2026-06-22

### Added
- **Release-version invariant + release-aware CHANGELOG gate** (#198, folds #177, epic #188): a release
  is an irreversible publish (ADR-0007). `publish.yml` now runs a **fail-closed preflight before the
  PyPI upload** — the release tag, `pyproject.version` and a non-empty `## [X.Y.Z]` CHANGELOG section
  must all agree (a duplicate version is backstopped by the non-skip upload). `release-close.yml` is
  **gated on publish success** (triggered by the Publish workflow completing `success`) so a reporter's
  issue is never closed "released and available now" before the package is actually live. New
  `scripts/ci/release_preflight.py` carries the pure logic (`release_preflight`, `changelog_release_state`)
  and a `--preflight` CLI that also checks PyPI. **Fixes #177:** promote.sh gate 3 demanded a non-empty
  `[Unreleased]`, which was mutually exclusive with the post-bump state doc-reality-audit demands
  (`pyproject == newest CHANGELOG`); the gate now classifies pending-dev and cut-release as valid and
  only genuine drift as fail, so the whole release flow runs. `test_release_preflight.py` (9).
- **Upstream round-trip + board reconcilers** (#194, epic #188): four invariants on the cross-repo
  and board derived views (ADR-0007). `upstream-closed-is-released` — a public `ironclad` issue that
  reached `resolved` may only be closed via delivery (which stamps `released`); a closed-without-
  `released` issue is drift and is healed (fixed the stranded `ironclad#5`). `open-assigned-in-progress`
  — an open + **assigned** issue must be at least In Progress on the board, healed by adding it and
  setting the column (closes the gap that left the *active epic* invisible while its sub-issues moved).
  `upstream-triaged-has-mirror` + `mirror-wiring-live` (warn) detect a stranded triage and a rotted
  intake. `mirror-from-public.yml` is hardened to **create the mirror before** labelling `triaged`
  (a partial failure can no longer strand a report), and `reconcile.yml` routes the upstream heals via
  `UPSTREAM_TOKEN` and board heals via `PROJECTS_TOKEN`. `test_process_doctor.py` (+7). 
- **Secret scan un-degraded** (#196, epic #188): the export secret-gate could pass **degraded** when
  gitleaks was absent (fail-open). `export_core.py --require-scanner` (+ `EXPORT_REQUIRE_SCANNER=1`)
  now **fail-closes** if no scanner ran; a new private CI `secret-scan` job installs gitleaks and
  exports with that flag, and the **public** `ironclad` CI gains a `gitleaks` job so a secret pushed
  straight to the public repo is caught too (the private gate can't see a public hand-push).
  `test_export_secret_gate.py` (2). Per ADR-0007.
- **Export↔public byte-equality check** (#195, epic #188): `scripts/ci/export_sync_check.py` +
  `export-sync-check.yml` (scheduled + on core/ push) assert the public `ironclad` repo is a faithful
  export of `core/` — LF-normalised (a CRLF-only diff is never drift). A file present in public that
  the export does not produce (a hand-edit) is always drift; when public's `.export-source` stamp ==
  HEAD the trees must be byte-identical. `publish_core.sh` now writes that source stamp on push so
  drift is distinguishable from the normal "main ahead of the published release" state.
  `test_export_sync_check.py` (5). Per ADR-0007.
- **DEV_LOOP self-consistency lint** (#197, epic #188): the binding control prompt is itself a
  derived view — process-doctor `devloop-self-consistent` asserts every `*.yml` it cites resolves to
  a real file and every `aktuell v<X>` equals the pyproject version. Fixed the live drift it caught:
  the ghost `mirror-to-dev.yml` ref (real one is `mirror-from-public.yml`), the stale `v0.0.7`
  pointers (→ v0.0.14), and the drifty hard-coded test counts (→ point to test-report.md).
  `test_process_doctor.py` (+3 = 15). Per ADR-0007.
- **Doc-lint coverage** (#193, epic #188): a process-doctor warn `open-milestone-has-description`
  (an open milestone with work but no description is invisible on the generated roadmap); the release
  gate (`promote.sh`) now runs `gen_roadmap.py --check` so a release can never ship a stale roadmap.
  docs-guide records the enforcement. `test_process_doctor.py` (+1 = 12). Per ADR-0007.
- **Issue/milestone invariants in process-doctor** (#192, epic #188): `delivered-milestone-closed`
  (a milestone with work done + 0 open issues must be closed so the generated roadmap drops it —
  assert + heal) and `open-epic-has-milestone` (an open `type/feature` epic with no milestone is
  invisible to the roadmap — **warn**, surfaced for operator triage; picking the milestone is a
  product call). Adds a `warn` tier to the check framework. `test_process_doctor.py` (+3 = 11).
  (Live: #75 surfaced for milestone triage.) Per ADR-0007.
- **Board invariant: closed issue ⇒ board Done** (#191, epic #188): the Projects-board card race
  (#176) is now structurally closed. `project-status.yml` gains a **closed-guard** that re-queries
  **live** issue state (not the event payload) and refuses to set In Progress on a closed issue;
  process-doctor gains a `board-closed-is-done` check (assert + heal) so the **scheduled** reconciler
  is the load-bearing healer that returns any stuck card to Done regardless of Action-run ordering.
  `test_process_doctor.py` (+3 = 8). Per ADR-0007.
- **process-doctor + scheduled reconciler backbone** (#190, epic #188): `scripts/ci/process_doctor.py`
  — an executable check registry that asserts the derived-view invariants against live GitHub state
  (`--check`) and heals them idempotently (`--reconcile`), fail-closed (a gh **auth** failure
  hard-fails, never soft-skips). Seed invariant: *a closed issue carries no `status/*` label* (assert
  + heal; the 12 live stale labels were reconciled). A scheduled `.github/workflows/reconcile.yml`
  runs the healer daily — the load-bearing leg that catches metadata-only mutations (Gap C: a
  milestone/label change triggers no path-gated push check). `gen_roadmap.py --check` now hard-fails
  on a gh auth failure (distinct from a network soft-skip). `test_process_doctor.py` (5). Per ADR-0007.
- **ADR-0007 — reconcilers + invariants over every derived view** (#189, epic #188): generalises
  ADR-0006 from the roadmap to the whole "derived view drifts from reality" class (board, issue/label/
  milestone metadata, mirror/upstream, public export, secret gate, DEV_LOOP). Each view gets an
  invariant + on-event guard (re-queried live state) + a **scheduled reconciler** (the load-bearing
  healer; idempotent, fail-closed), all asserted by an executable `process-doctor` with a negative
  test per invariant. Records the Wave-1 scope + the deferred follow-ups (deep CI required-checks,
  deploy/spark prod).

## [0.0.14] - 2026-06-22

### Changed
- **Roadmap rule: open-milestone (not open-epic)** (#185, epic #169): the generated roadmap now
  renders one section per **open milestone** (its description = the phase narrative); a delivered
  phase drops by **closing its milestone** (M3 + M6 closed). This refines #176's initial
  "milestone with ≥1 open epic" rule, which would have erased an active phase the moment its current
  epics merged (e.g. M5 vanishing when epic #169 itself closed). ADR-0006 D2/D3, `docs-guide.md`,
  and the DEV_LOOP/NEW_EPIC C2 prune wording updated to match.

### Added
- **Generated, drift-proof roadmap** (#176, epic #169): `roadmap.md` is now produced by
  `scripts/ci/gen_roadmap.py` from the **open roadmap phases** (open milestones with ≥1 open
  `type/feature` epic) — a phase drops off automatically once its epics close, so realized work can
  never linger (the structural fix). The per-phase narrative lives in the **milestone description**
  (the single editable source); the generator only renders. A CI job (`roadmap-generated`)
  regenerates and fails on drift (soft-skips if the GitHub API is unavailable; the offline per-doc
  lint is the always-on guard). Pure `render_roadmap` is unit-tested; `test_gen_roadmap.py` (5).
  Per ADR-0006.
- **doc-reality-audit: per-doc responsibility lint** (#174, epic #169): a new fail-closed check —
  `roadmap.md` must contain no realized markers (`shipped`/`delivered`/`wired + tested`/`now|generally
  available`) and `status.md` no future markers (`coming soon`/`will ship`). File-scoped + tiny
  marker lists so legitimate usage (status.md's own `wired + tested`, `see the roadmap` pointers) is
  never flagged. Covered by `test_doc_audit.py` (9) incl. the **negative test** (a deliberately
  "realized" roadmap item makes the audit FAIL) + a real-docs-pass regression. Per ADR-0006.

### Changed
- **README reconciled to the doc IA** (#173, epic #169): the `## Roadmap` section is now a clean
  pointer (planned → roadmap.md, runs-now → status.md) instead of a duplicated, drift-prone
  "done vs planned" list — fixing the stale claim that roadmap.md shows "what works today" and the
  outdated "Phase g" reference. README stays intro/quickstart/value-prop; the per-component wiring
  matrix lives only in status.md (no overlap). (Repo hygiene: the untracked root-level duplicate of
  `vault/Plan/plan_skill_libary.md` — a byte-identical local stray — was removed; the tracked copy
  is the canonical one.)
- **roadmap.md is now future-only** (#172, epic #169): removed the realized content that violated the
  "forward-looking only" contract — the delivered skill-generation engine (ADR-0001/0002), the
  prompt library + discovery/invocation (ADR-0003/0005), and the shipped Extension SDK (ADR-0004),
  plus the two fully-closed phases (skill-generation, prompt-library usability). That content lives in
  `status.md` (wiring SSOT) + `CHANGELOG` history. Roadmap now carries only open themes (enterprise,
  connectors, broader model/data, release maturity). Deferred-but-unscoped items (skill/prompt
  curation, save-as-item) have no open epic yet → tracked in [ADR-0005](docs/adr/0005-prompt-skill-discovery-invocation.md), will reappear on the roadmap when epic'd. Per ADR-0006.

### Added
- **Documentation guide (`docs/docs-guide.md`)** (#171, epic #169): a contributor-facing
  "where does this go?" reference — one responsibility per doc (README=intro, status=now/SSOT,
  roadmap=future-only/generated, CHANGELOG=history, ADRs=decisions), a decision guide, and the
  enforcement summary. Linked from README; README's roadmap pointer corrected ("what works today vs
  planned" → "planned or in progress, future only"). Per [ADR-0006](docs/adr/0006-docs-ia-and-drift-proof-roadmap.md).
- **ADR-0006 — documentation IA + drift-proof generated roadmap** (#170, epic #169): records the
  root cause of recurring doc drift (the audit has no notion of "realized"; prune-on-close is a
  manual, gateless step) and the fix — one responsibility per doc (README=intro, status=now/SSOT,
  roadmap=future-only, CHANGELOG=history), a **generated** roadmap (from open top-level epics, so a
  closed epic auto-drops), prune-on-close as a C2 gate, and a two-layer audit (offline
  forward-only/per-doc lint + a generation check). Implementation follows in #171–#176.

## [0.0.13] - 2026-06-22

### Added
- **ADR-0005 + roadmap phase 6** (#161, epic #146): records the discovery + per-item invocation +
  `/catalogue` design as [ADR-0005](docs/adr/0005-prompt-skill-discovery-invocation.md) (closing the
  [ADR-0003](docs/adr/0003-prompt-library.md) D5 gap), and adds roadmap **phase 6** "Skill & prompt
  library — usability & content" (the shipped usability/seed foundation + the forward-looking
  curation/maintenance flow). roadmap.md phase 3 marked delivered.
- **Three curated prompt-library seeds** (#150, epic #146): `pr-description`, `refactor-plan`, and
  `test-plan` (engineering, EN+DE) bring the curated starter set under `skills/prompts/` to
  **7**. Each is one declarative `kind: prompt` MD file (+ a `locales/de.json` overlay), passes
  `ack.gate`, and is visible via `/prompts` + invocable via `/<name>` purely by dropping the file —
  no engine change. `test_prompt_library.py` now parametrises over all 7 (24).
- **Catalogue endpoint + dynamic slash autocomplete** (#149, epic #146): a guarded `GET /catalogue`
  serves the loaded registry snapshot (the same `_catalogue_snapshot` that backs `/prompts`/`/skills`
  — one surface). It is gated like `/tasks`/`/doctor` (added to `GATED_PATHS`; 401 without the
  deployment secret under token/sealed). The TypeScript client fetches it lazily on the first
  slash-menu open and merges loaded **prompt** names into autocomplete as directly-invocable
  `/<name>` entries — a built-in command wins on a name collision; skills (not bare-slash invocable)
  are intentionally not injected. Fail-soft: an older server / gated session → built-in commands
  only. `test_catalogue_endpoint.py` (3) + `catalogue.test.ts` (6).
- **Per-item prompt invocation `/<prompt-name>`** (#148, epic #146): the command router resolves a
  `/<prompt-name>` against the loaded prompt catalogue and runs it deterministically (model-free) —
  parses `var=value` / a single positional / `--lang xx`, and reuses the `ack.promptgen` elicitation
  state machine: assembles the finished prompt in the target language when all required variables are
  present, else returns the guiding questions for what is missing. Resolution runs **after** every
  built-in command, so a real command is never shadowed; an unknown `/x` still falls through to a
  model turn. The model-elected `use_prompt` tool stays available (additive). A single positional value fills the
  lone required variable verbatim (a `=`/`--lang` inside a code/diff value is preserved), with a
  trailing `--lang xx` peeled; explicit `var=value` sets named variables. `test_prompt_invocation.py` (14).
- **Discovery commands `/prompts` + `/skills`** (#147, epic #146): read-only listing of the **one
  loaded registry** (no re-scan, no parallel mechanism). `/prompts` lists every loaded
  `kind: prompt` item (name, declared languages, description); `/skills` lists every loaded skill
  across both kinds — `SKILL.md` playbooks and typed `CASE`+`run` tools (incl. the MPR built-in).
  Both are advertised in `/help` and offered in the TypeScript client's slash autocomplete. Backed
  by a shared `_catalogue_snapshot` helper over `_PROMPTS`/`_PLAYBOOKS`/`_PLUGIN_TOOLS`.
  `test_discovery_cmds.py` (6) + `classify.test.ts`.

## [0.0.12] - 2026-06-22

### Changed
- **Internal DEV→Prod→Public promote finalized** (#40, epic #132): the release pipeline now runs as
  a single fail-closed gated flow — boundary → **full** test suite (incl. the MPR built-in, which
  the previous step missed) → docs gate (CHANGELOG + doc-reality-audit) → export gates →
  **PRE-publish clean-room** (wheel → fresh venv → import-smoke incl. `ack.sdk` → an example plugin
  builds against the installed SDK) → review → publish → prod redeploy; dry by default. Full
  automation (scheduled sync) deferred. (Core-maintainer machinery; downstream users never touch it.)

### Added
- **Example plugin + SDK clean-room guarantee** (#138, epic #132): a standalone example plugin
  (`examples/example-plugin/`) shows the separate-repo authoring shape — a package with a `skills/`
  dir + an `ironclad.plugins` entry point, built against `ironclad-ai`. The clean-room now
  import-smokes `ack.sdk` and **builds the example against the freshly-installed wheel**, asserting
  it registers its entry point, runs, and matches the SDK schema — proving a separate repo can build
  against the published artifact. 3 tests (skip in installed trees; the workflow covers that path).
- **Export-leak guard for internal plugins** (#137, epic #132): the boundary check + export
  secret-sweep now forbid the internal plugin repo name, and a leak-guard test pins the guarantee
  (the guards flag a synthetic leak; the real `core/` + `clients/ink` tree is clean). `core/`
  couples to plugins only via the generic `ironclad.plugins` entry-point group — never a concrete
  private plugin. (The guards live in `scripts/ci/`, private; the test skips in installed/clean-room
  trees where they're absent.) 4 tests.
- **Packaged-plugin loading via entry points** (#136, epic #132): a pip-installed plugin
  (3rd-party or internal) is discovered at startup through the `ironclad.plugins` **entry-point
  group** — additively alongside built-ins + `GX10_PLUGINS_DIR`, with no path config and no core
  change. Dependency-inverted: the engine resolves each entry point to a plugins dir
  (package / callable / path) and scans it; it **never imports a concrete plugin**. Broken entry
  points are fail-soft. [ADR-0004](docs/adr/0004-extension-sdk.md), `plugin-api.md`. 10 tests.
- **Extension SDK (`ack.sdk`)** (#72, epic #132): a curated, versioned import surface to build a
  plugin in a **separate repo** against `pip install ironclad-ai` — re-exports the tool/playbook/
  prompt kinds, the registration/eval `gate`, `derive_tool_schema`, `Localizer`, and the
  `catalogue`. `ack.sdk.__all__` **is** the public API; everything else under `ack.*`/`engine.*`
  is internal. Provisional while `0.0.x`, semver from 1.0. [ADR-0004](docs/adr/0004-extension-sdk.md)
  + `plugin-api.md` (separate-repo workflow). 7 tests. *(The contract modules already shipped in
  the `ironclad-ai` wheel; this adds the curated surface, the stability policy, and the docs — no
  distribution change. The packaged-plugin `ironclad.plugins` entry-point seam is in development, #136.)*

## [0.0.11] - 2026-06-21

### Added
- **Curated multilingual starter prompt library + eval gate** (#111): four built-in `kind: prompt`
  items ship under `skills/prompts/` — `code-review`, `commit-message`, `bug-report`,
  `explain-code` (EN + DE, each loaded at startup and offered via `use_prompt`). New
  `ack.gate.gate_prompt` is the registration/eval gate for prompt items (required vars must appear
  in the template; every declared language must assemble cleanly; present locale overlays validated)
  and `ack.gate.gate()` auto-routes `kind: prompt` SKILL.md here. `discover_playbooks` now skips
  prompt items cleanly. **A new prompt = drop an MD file, no engine change.** 20 tests.
- **Prompt slash surface & guided elicitation** (`use_prompt`, #110): a discovered `kind: prompt`
  item is exposed as an engine tool — call with no capability to **list**, or with a capability +
  a `values` JSON of what's collected so far to drive **guided elicitation** (the tool returns the
  next missing required variable's question, one at a time) and, once complete, the **assembled**
  prompt in the target `lang` (preview). Wired through `_load_skills`/`_effective_tools`/dispatch;
  the state machine is `ack.promptgen.run_prompt` (deterministic, LLM-free). 9 tests.
- **Multilingual prompt assembly** (`ack.promptgen`, #109): `assemble(prompt, values, lang)`
  renders a `kind: prompt` template + collected values into a finished prompt in a target
  language via `ack.i18n` (per-item `locales/`, source/target + fallback); `missing_required()`
  drives the elicitation loop. Deterministic, LLM-free. 6 tests.
- **Prompt-library item format** (`ack.prompt`, `kind: prompt`, #108): parse/validate/discover a
  declarative prompt item (variables + languages + per-variable elicitation), reusing the shared
  `ack.playbook` frontmatter parser (one parser, no parallel infra). A prompt is a core built-in,
  distinct from `kind: playbook`. 7 tests.
- **Design: prompt library & generator** ([ADR-0003](docs/adr/0003-prompt-library.md) +
  [`prompt-packaging.md`](docs/prompt-packaging.md), epic #105) — a curated, multilingual prompt
  library on the core base: a prompt is a declarative `kind: prompt` core built-in (variables +
  languages + guided elicitation), reusing `ack.playbook`/`ack.catalogue`/`ack.gate`/`ack.i18n`;
  `/<prompt-name>` → elicit → multilingual assembly → preview. Design only — built under epic #105.

## [0.0.10] - 2026-06-21

### Changed
- **Export/deploy/docs aligned to core MPR** (#116): `export_core.py` drops the separate MPR
  bundling (MPR ships via the core/ copy + is covered by the core boundary check); the install
  launchers no longer set `GX10_MPR`; the installer no longer copies a separate `skills/mpr`; the
  MPR README documents the single runtime `mpr.enabled` gate. Finalizes the `GX10_MPR` deprecation.
- **MPR is now a core, always-on built-in** (#115): moved `skills/mpr` → `skills/mpr`;
  removed the `GX10_MPR` load gate (MPR is always loaded) — the live on/off is the runtime
  config **`mpr.enabled` (default ON)**. MPR consumes the core registry + `ack.i18n` + catalogue
  + gate. Back-compatible (behavior unchanged when enabled); its suite (381) is now part of the
  core `pytest` run, so the private CI gates it too. **Deprecation:** `GX10_MPR` is gone — use
  `mpr.enabled` (or `GX10_MPR_ENABLED`) instead.

### Added
- **Always-on core built-in loader** (#114): built-in skills/prompts load at startup from a
  fixed core dir (`skills/`), scanned **unconditionally** — independent of
  `GX10_PLUGINS_DIR`, which stays the **additive** surface for 3rd-party/user skills
  (`_load_skills`). Built-ins now work out of the box with no config. 4 tests.
- **Shared content i18n `ack.i18n`** (#107): the file-overlay locale loader is promoted to core as
  `Localizer(locales_dir)` — flag-independent (always importable, no `GX10_MPR`/plugin coupling),
  parameterized locales dir, English fallback. MPR migrated onto it (`skills/mpr/i18n.py` is now a
  thin shim; 382 tests green, behavior unchanged). Distinct from `engine/messages.py` (engine
  chrome). 6 tests. Part of the core-always-on rebuild (ADR-0002).
- **Design: skill/prompt/MPR as core always-on** ([ADR-0002](docs/adr/0002-core-always-on-skills.md),
  epic #112) — built-ins load from a fixed core dir independent of `GX10_PLUGINS_DIR`; the plugin
  surface stays for 3rd-party skills; MPR de-plugined into core (runtime `mpr.enabled`, default on,
  replacing the `GX10_MPR` boot flag). Design only — implemented under epic #112.

## [0.0.9] - 2026-06-21

### Added
- **Skill lifecycle verified end-to-end** (#88): a model-free integration test drives the full
  pipeline for both kinds — `ack.skillgen` generate → `ack.gate` registration gate →
  `ack.catalogue` install/register → engine load (`_load_plugins`/`_load_playbooks`) → invoke
  (a typed tool returns a real result; a playbook loads via `use_skill`). This is the epic #22
  C2 runnable scenario. 3 tests.

### Changed
- **`mpr` migrated as the reference built-in** (#90): its `CASE` now carries the catalogue
  manifest fields (`type`/`version`/`provenance`), so `ack.catalogue` indexes it as a built-in
  skill — proving the generalized format is a superset of the real flagship. Additive +
  back-compatible (byte-identical when gated off; mpr suite 382 green).

### Added
- **Skill registration gate** (`ack.gate`, #34): no unchecked skill enters the toolset. A tool
  must pass a doctor preflight (loadable, `CASE`+`capability`, synchronous `run`, derivable tool
  schema) and ship an auto-generated test; a playbook must have valid `SKILL.md` frontmatter +
  readable references + a passing `scripts/check`. Behavioral `eval/` stays opt-in. 7 tests. Also
  made the scaffolded playbook `scripts/check` self-contained (no import path assumptions).
- **Skill library catalogue** (`ack.catalogue`, #35): a self-hosted, versioned index over both
  skill kinds, reading each skill's own metadata as its manifest (`capability`/`kind`/`version`/
  `type`/`domain`/`provenance`/`source`). Discover, install (copy into the active `skills/`),
  and update-when-newer (semver), with provenance and built-in vs user libraries — no external
  marketplace. Zero new deps; 6 tests.
- **Skill generator** (`ack.skillgen`, #33): `spec → schema-valid scaffold` for both skill
  kinds — a typed `CASE`+`run` `.py` (signature → tool schema) with an auto-test stub, or a
  `SKILL.md` playbook package + `references/` + `scripts/check`. Contract-correct by
  construction; the body is a marked stub for an author/LLM to fill. CLI `python -m ack.skillgen`.
  Zero new deps; 7 tests.
- **Playbook skill kind** (`SKILL.md` packages, ADR-0001 / #89): a second skill kind alongside
  the typed `CASE`+`run` tool. `ack.playbook` parses + validates `SKILL.md` frontmatter and
  `Registry.discover_playbooks` discovers packages; the engine exposes them via the new
  **`use_skill`** tool with **progressive disclosure** (list metadata → load body → load a
  reference on demand). Zero new dependencies; 15 deterministic tests (`test_playbook.py`).
- **Skill-engine design**: [ADR-0001](docs/adr/0001-skill-engine-and-library.md) +
  [`skill-packaging.md`](docs/skill-packaging.md) — the design for the skill-generation engine
  & self-hosted library (two skill kinds: typed `CASE`+`run` tools and `SKILL.md` playbooks;
  doctor+tests registration gate with opt-in behavioral eval; manifest catalogue with semver +
  provenance; `skills/mpr` as the reference built-in). Design only — built under epic #22.

## [0.0.8] - 2026-06-21

### Added
- **Public-release clean-room gate** (`.github/workflows/clean-room.yml`, #58). Before any
  publish, the package is proven installable + runnable **from the published sources in
  isolation**: PRE-publish builds the wheel, installs it into a fresh venv (not `-e`),
  import-smokes from a neutral dir and resolves the `[engine]`/`[memory]` extras, and does a
  fresh `clients/ink` build + test; POST-publish installs the real package from PyPI and
  import-smokes. Catches forgotten dependencies/files before a user hits them.

### Fixed
- **Published doc link** (`engine/README.md` → the bundled `clients/ink/` client) now
  resolves in the released tree; the export now normalizes the `../` depth of links to the
  bundled siblings (`clients/ink`, `skills/mpr`), and a deterministic doc-reality audit gate
  guards against dead links/anchors, version drift, and stale claims going forward (#41).

### Changed
- **Docs: release-status & test counts reconciled to reality** (#59). The status/honesty
  sections (`README.md`, `docs/status.md`, `docs/roadmap.md`, `SETUP.md`) now state the
  actual release model — pre-release `0.0.x` alpha, tagged releases on PyPI (`ironclad-ai`)
  **and** GitHub Releases (`v0.0.7`), no stable 1.0 — instead of the stale "no tagged
  release". Test counts regenerated from `pytest --collect-only`: **468** Python
  (459 offline + 9 live) and **337** TypeScript client tests; `docs/test-report.md` area
  table updated to match.
- **English-only hygiene**: translated the remaining German code comments in the bundled
  test suite (`ack/tests/test_autoplan.py`, `test_client_pool.py`, `test_workers.py`) to
  English (#78). Deliberate `language=de` user-facing output and the German query-classifier
  keywords are intentionally kept.

## [0.0.7] - 2026-06-21

### Changed
- **Installer is type-aware + config-driven.** The `ironclad`/`ironclad-doctor` launchers now support both a
  local engine (`type: desktop`, default) and a thin client against a remote orchestrator (`type: spark`),
  and read optional tuning (`engineConfig`, `warmUrl`, `claudeBin`, `fanoutConcurrency`, `workersMaxTokens`,
  `workersMaxBatchTokens`) from the project config — absent → engine defaults, so a deployment can tune
  without editing scripts. Adds `install/ironclad-commands.ps1` (profile shim). All still secret-free.

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

## [0.0.2] - 2026-06-18

### Added
- Bundled **TypeScript terminal client** (`clients/ink`): purpose-built renderer (ghost-free resize,
  native scrollback/selection/copy, live-streaming markdown, Ctrl+F search), global install
  (`npm install -g .` → `ironclad`), JSON config file (file < env < flags), Esc/Ctrl+C turn-cancel.

### Changed
- Docs reconciled to the recommended-client + global-install flow; onboarding paths made
  export-relative. The Python ACK + orchestration engine are unchanged in behaviour (version bump only).

## [0.0.1] - 2026-06-17

### Added
- First public pre-release: reliability for LLM agents through enforcement, not model size — the
  Agent-Contract-Kernel + a fail-closed orchestration engine, server/client split, full-screen TUI,
  reasoning-worker fan-out, optional Mem0 memory, and a one-command compose. Model-agnostic (any
  OpenAI-compatible endpoint); PyPI name `ironclad-ai`. See `docs/status.md` for per-component wiring.
