# Changelog

All notable changes to Ironclad are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/); the project is **pre-release** (0.0.x).
Released versions are listed below; upcoming work accumulates under *Unreleased*.

> Docs are code: a change does not ship without a Changelog entry (the promote gate
> enforces a non-empty *Unreleased* section).

## [Unreleased]

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
