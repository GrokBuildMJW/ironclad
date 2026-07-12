# ADR-0016: Constraint compliance L2/L3 — typed fields, hard/soft, detect vs MPR flags

## Status

Accepted — foundational plumbing (#1341 / epic #1344 S5), L2 detect + durable fork-envelope emission
(#1337 / S3), L2 operator surface + durable project-scoped MPR worker + decide→learn
(#1340 / S4), L3 fail-closed typed hard-check at the implementation boundary
(#1342 / S6), **and** the L2/L3 real-dispatch E2E capstone (#1359 / S7). Default off.

**Status: Superseded (in part) by ADR-0006 (#1414)** — the product presence gate / L2 conflict-fork /
L3 typed HARD-floor described here was retired in S1; `record_constraints` is now optional non-gating
framing-note capture; build enforcement is the approved-design anti-drift. The build-boundary hard-check
and verbatim injection survive.

## Context

L1 (#1319 / ADR-0015) makes constraints **present**: a single canonical `decisions/constraints.md`, a
presence gate, and verbatim handover injection. That is necessary but not sufficient for compliance:

- A design can still silently diverge from a recorded language/network floor (the motivating DEV-1
  defect: `Constraints: Python` → web search → design chooses Rust).
- Free-form prose cannot be compared deterministically; only a small machine-checkable subset can be
  a fail-closed hard-check.
- Model-heuristic extractions must not gate until an operator promotes them; provenance must be
  explicit and shared across L2 (advisory conflict fork) and L3 (structured hard-check).

Epic #1344 therefore adds **L2** (advisory conflict fork via ACE/MPR) and **L3** (typed hard-check).
This unit is the **shared foundation**: typed allow-list, capture plumbing, hard/soft classifier,
and default-off flags — without detector, worker surface, compare, or gate.

## Decision

### D1 — Frozen typed allow-list

Only two machine-checkable keys exist today: `language` and `network` (`TYPED_KEYS`). Alias tables
are frozen in pure `ack.ace.constraint_types` (e.g. `py|python3 → python`, `none|forbidden → False`).
Unknown values normalize to `None` and never raise.

### D2 — Pure ACK module (boundary-clean)

Normalization, `parse_typed`, marker detection, and `classify` live in `ack/ace/constraint_types.py`.
No engine import. The engine may only call these helpers and persist results; S3/S6 must not reimplement
alias tables or provenance rules.

### D3 — Capture plumbing (optional, fail-closed)

`record_constraints` and `record_design` accept optional `language` / `network` tool params. Values are
normalized and written as frontmatter **only when provided and valid**. An invalid provided value is a
`GateRefusal` at capture (fail-closed). Omitting the params leaves S1 frontmatter **byte-identical**.

### D4 — Hard / soft provenance

- **HARD** (`source: hard`): an explicit `Constraints:` marker in the body, or a typed param supplied
  without a model-heuristic source.
- **SUGGESTED** (`source: suggested`): model heuristic only (`source="suggested"`); typed values may
  still be recorded but are **excluded** from the HARD typed reader until promotion
  (`/approve constraint` — S4 / #1340, not this unit).

`_constraint_typed(slug) → dict` returns only HARD typed values (fail-soft `{}`). It is the single
pre-filter S3 and S6 will read.

### D5 — TaskSpec optional fields

`TaskSpec` declares optional `language: Optional[str]` and `network: Optional[bool]` because
`extra="forbid"`. Absent keys validate exactly as today. No conditional-required logic yet (S6).

### D6 — Default-off; detect is opt-in (S3); surface + worker are S4

S5 plumbs data + provenance + flags only. **S3 (#1337)** wires L2 detect under
`CONSTRAINT_CONFLICT_DETECT`: on a typed HARD mismatch at `record_design`, a pure
`ack.ace.constraint_conflict.detect_conflict` fires and the engine persists a pending
`ForkEnvelope` (`ack.ace.fork_envelope`) under `vault/<slug>/proposals/forks/<fork_id>.json`
(idempotent by opaque `fork_id`; free-text `question` excluded from identity). Flag off remains
**byte-identical**.

**S4 (#1340)** owns the operator surface and the durable project-scoped MPR worker:

- Optional MPR `artifact_slug` port routes `runs_dir` / gate / INDEX to the envelope's initiative
  (never drain-time `active_slug()`); `None` is byte-identical.
- Worker (gated by `ace.fork_mpr.enabled`) captures `contextvars.copy_context()` at submit and runs
  each item via `ctx.run` (ReflectionWorker is a long-lived daemon — not bind-at-start). Safe-queue
  run lock is **process-local and non-durable** (in-memory set keyed by `fork_id`, mirrors M5
  `_ACE_FORK_INFLIGHT`) — never persisted `inflight` on the envelope — so a hard crash never leaves
  the envelope permanently claimed (#17). Durable "needs MPR" state is `status==pending` and
  `recommendation is None`. Fill `recommendation`/`matrix` on success (status stays `pending`);
  release the run lock on failure (retry later).
- `/fork` / `/fork list` renders pending envelopes (opaque ids, latest per `(slug, category)` with
  supersession) and includes concrete ready-to-run `/fork decide <fork-id> --choice keep|counter`
  commands with the real opaque `fork_id`; `/fork decide <fork-id> --choice keep|counter` is the R5
  state machine (fail-closed, idempotent). `keep` leaves constraints.md unchanged and clears the
  rejected counter from `design.md` or restores the prior compliant design so approval is not
  dead-locked; `counter` overrides the typed HARD value with provenance `operator-override` and
  promotes the envelope's counter design body into `design.md`.
- Constraint `ForkEnvelope` records may carry `counter_design` (the full parked counter-proposal
  `design.md` body) and `restore_design` (the pre-overwrite compliant `design.md` body, when one
  existed). These fields are not part of `fork_id` identity.
- `/approve design [slug]` (bare `/approve` preserved) is blocked while a pending constraint fork
  exists; when `CONSTRAINT_CONFLICT_DETECT` is on, a ledger-read failure also refuses (fail-closed).
  Flag off ⇒ no ledger block (byte-identical). `/approve constraint <id|all> [--slug]` promotes
  suggested → hard.
- decide→learn feeds ACE from the envelope resolution (fail-soft, off hot path).

Both flags off ⇒ **byte-identical** (no worker, `/fork` falls through to M5 unit proposals, no
`/approve` blocking, no hard-check).

### D6b — L3 hard-check at the implementation boundary (S6 / #1342)

Pure `ack.ace.constraint_conflict.hardcheck(constraint_typed, provided_typed, *, require_present)`
returns a frozen `Violation` (`kind=missing|mismatch`) for the first `TYPED_KEYS` HARD-floor
failure, or `None`. Distinct from `detect_conflict`: omission is a violation when
`require_present=True` (closes the omission bypass). Engine thin wrapper
`_constraint_hardcheck` returns an ERROR string or `None`; flag off / no HARD typed floor →
`None` (byte-identical).

**Hard floor sites** (IMPLEMENTATION boundary only):

1. **`/approve design`** — after the S4 pending-fork block, before stamping `approved: true`;
   design typed fields via `parse_typed(design.md)`.
2. **Impl `stage_handover`** (create + re-hand) — PRE-write; task typed fields from TaskSpec
   `language`/`network` on the real task object (not design frontmatter alone).
3. **`plan_units` children** — PRE-write, atomic; same task-typed compare; `force` does not bypass.

**Advisory only:** `record_design` still only emits the L2 fork (S3) and records the proposal —
a conflicting design can be recorded but cannot be approved or implemented until consistent.
Couples with S4 decide: `keep` leaves the floor unchanged and makes `design.md` coherent by restoring
or clearing the rejected counter; `counter` overrides the floor to the design's value and promotes the
counter body (approval proceeds).

### D7 — Flag split (detect vs MPR worker)

| Flag | Config | Default | Role |
|------|--------|---------|------|
| `CONSTRAINT_CONFLICT_DETECT` | `safety.constraint_conflict_detect` | `false` | Gates L2 **detect** (S3) and L3 **hard-check** (S6). Strict `_as_bool`. |
| `_ACE_FORK_MPR` (existing) | `ace.fork_mpr.enabled` | `false` | Gates the **MPR worker** path (M5 fork signals **and** S4 constraint envelopes + decide→learn). Reused — not a new flag. |

Both default-off ⇒ public installs remain byte-identical. Detect-without-MPR and MPR-without-detect are
independent; operators enable each deliberately.

### D8 — Schema surface

New tool parameters are **optional**. With flags off and params omitted, lifecycle behaviour and
constraint frontmatter match L1. The model may see the optional properties on `record_design` (always
offered) and on `record_constraints` (only when `constraint_gate.enabled` exposes that tool).

## Scope map (epic #1344)

| Layer | What | Sub / issue |
|-------|------|-------------|
| L1 (shipped) | Capture, presence gate, verbatim injection | #1319 / ADR-0015 |
| **S5 plumbing (this ADR)** | Typed fields, classify, flags | **#1341** |
| **L2 detect (this ADR / S3)** | Structured conflict → durable `ForkEnvelope` ledger | **S3 / #1337** |
| **L2 surface (this ADR / S4)** | Recommend → decide → learn; `/fork` + `/approve constraint\|design` | **S4 / #1340** |
| **L3 hard-check (this ADR / S6)** | Design/task typed vs HARD typed fail-closed compare at approve + impl | **S6 / #1342** |
| **L2/L3 E2E capstone (S7)** | Real-dispatch keep/counter/omission + gate-off byte-identical | **S7 / #1359** |

## Requirements (R1–R6)

- **R1** Pure ACK classifier/normalizer; engine is a thin persist/read layer.
- **R2** Invalid typed input fails closed at capture; missing typed input is not an error.
- **R3** Suggested never enters `_constraint_typed` until promotion (S4).
- **R4** Default-off flags; strict boolean coercion (no `bool("false")` trap).
- **R5** English-only, boundary-clean export; no private literals.
- **R6** No silent behaviour change when flags are off and typed params are absent (byte-identical).

## Consequences

- S3 reads HARD typed floors via `_constraint_typed` without re-parsing prose or inventing aliases.
- S3 persists pending fork envelopes; S4 fills recommendation/matrix, lists/decides, and learns.
- S6 compares design/task typed fields to `_constraint_typed` deterministically; S4 decide
  (keep vs counter) is the operator path that aligns the floor or the design before the hard
  floor opens.
- S4 owns promotion of `suggested → hard` (`/approve constraint`) and the operator command surface.
- Deployments that never enable `safety.constraint_conflict_detect` / `ace.fork_mpr.enabled` keep
  L1-only behaviour.
- Extending the allow-list later is an explicit contract change (new key + normalize + tests + ADR note).

## References

- ADR-0015 — L1 constraint-aware design gate (capture / presence / injection).
- Epic #1344 — L2 advisory conflict fork + L3 structured hard-check.
- #1341 — foundation (typed fields + classifier + flags).
- #1337 — L2 structured detector + durable fork-envelope emission (S3).
- #1340 — L2 durable project-scoped fork worker + `/fork` surface + decide→learn (S4).
- #1342 — L3 structured hard-check at the implementation boundary (S6).
