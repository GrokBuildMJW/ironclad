# ADR-0016: Constraint compliance L2/L3 — typed fields, hard/soft, detect vs MPR flags

## Status

Historical decision — foundational plumbing (typed fields + classifier + flags), L2 detect + durable
fork-envelope emission, L2 operator surface + durable project-scoped MPR worker + decide→learn,
L3 fail-closed typed hard-check at the implementation boundary, **and** the L2/L3 real-dispatch
E2E capstone.

**Status: Superseded by ADR-0006, except for the approved-design build boundary.** The product
presence gate, L2 constraint-conflict fork, and constraint-ledger HARD floor described historically below
were retired. `record_constraints` is now optional non-gating framing-note capture. The surviving mandatory
boundary validates and injects the approved design standard; it has no enable switch.

Completion is a separate always-on protected boundary. After feedback-file presence is established,
`advance_pipeline` requires the selected artifact to be readable, non-empty, and explicitly normalized to
`status: done` before egress checks or the task transition. No absent, malformed, unknown, blocked, or
clarification-needed signal is equivalent to completion. The retired `advance_gate.enabled` key is only a
warning tombstone and cannot restore the former bypass.

Egress is also a separate always-on protected boundary. Its only policy input is the approved
design's `## Build policy`: no section is non-build-enforcing, explicit `network: open` skips analyzers, and
`network: none|declared` runs them. A present section with a missing/invalid posture, no usable code root, or
an analyzer/import/internal failure under a restrictive posture refuses advance. The retired
`security.egress_analysis.enabled` key and `GX10_EGRESS_ANALYSIS_ENABLED` alias are tombstones, not switches.

## Context

L1 (ADR-0015) makes constraints **present**: a single canonical `decisions/constraints.md`, a
presence gate, and verbatim handover injection. That is necessary but not sufficient for compliance:

- A design can still silently diverge from a recorded language/network floor (the motivating
  defect: `Constraints: Python` → web search → design chooses Rust).
- Free-form prose cannot be compared deterministically; only a small machine-checkable subset can be
  a fail-closed hard-check.
- Model-heuristic extractions must not gate until an operator promotes them; provenance must be
  explicit and shared across L2 (advisory conflict fork) and L3 (structured hard-check).

The follow-on work originally added **L2** (advisory conflict fork via ACE/MPR) and **L3** (typed hard-check).
The remainder of this ADR records that historical design. Current behavior is governed by the supersession
notice above and the reconciliation notes in D6-D8 below.

## Decision

### D1 — Frozen typed allow-list

Only two machine-checkable keys exist today: `language` and `network` (`TYPED_KEYS`). Alias tables
are frozen in pure `ack.ace.constraint_types` (e.g. `py|python3 → python`, `none|forbidden → False`).
Unknown values normalize to `None` and never raise.

### D2 — Pure ACK module (boundary-clean)

Normalization, `parse_typed`, marker detection, and `classify` live in `ack/ace/constraint_types.py`.
No engine import. The engine may only call these helpers and persist results; the later increments must not
reimplement alias tables or provenance rules.

### D3 — Capture plumbing (optional, fail-closed)

`record_constraints` and `record_design` accept optional `language` / `network` tool params. Values are
normalized and written as frontmatter **only when provided and valid**. An invalid provided value is a
`GateRefusal` at capture (fail-closed). Omitting the params leaves the base capture frontmatter **byte-identical**.

### D4 — Hard / soft provenance

- **HARD** (`source: hard`): an explicit `Constraints:` marker in the body, or a typed param supplied
  without a model-heuristic source.
- **SUGGESTED** (`source: suggested`): model heuristic only (`source="suggested"`); typed values may
  still be recorded but are **excluded** from the HARD typed reader until promotion
  (`/approve constraint`, delivered as a separate unit, not this one).

`_constraint_typed(slug) → dict` returns only HARD typed values (fail-soft `{}`). It is the single
pre-filter the detector and hard-check layers will read.

### D5 — TaskSpec optional fields

`TaskSpec` declares optional `language: Optional[str]` and `network: Optional[bool]` because
`extra="forbid"`. Absent keys validate exactly as today. No conditional-required logic yet.

### D6 — Historical opt-in detector and worker (superseded)

The following was the original behavior. It is not the current switch surface:

The plumbing increment supplies data + provenance + flags only. The **detector increment** wires L2 detect
under `CONSTRAINT_CONFLICT_DETECT`: on a typed HARD mismatch at `record_design`, a pure
`ack.ace.constraint_conflict.detect_conflict` fires and the engine persists a pending
`ForkEnvelope` (`ack.ace.fork_envelope`) under `vault/<slug>/proposals/forks/<fork_id>.json`
(idempotent by opaque `fork_id`; free-text `question` excluded from identity). Flag off remains
**byte-identical**.

The **operator-surface increment** owns the operator surface and the durable project-scoped MPR worker:

- Optional MPR `artifact_slug` port routes `runs_dir` / gate / INDEX to the envelope's initiative
  (never drain-time `active_slug()`); `None` is byte-identical.
- Worker (gated by `ace.fork_mpr.enabled`) captures `contextvars.copy_context()` at submit and runs
  each item via `ctx.run` (ReflectionWorker is a long-lived daemon — not bind-at-start). Safe-queue
  run lock is **process-local and non-durable** (in-memory set keyed by `fork_id`, mirrors the
  `_ACE_FORK_INFLIGHT` guard) — never persisted `inflight` on the envelope — so a hard crash never leaves
  the envelope permanently claimed. Durable "needs MPR" state is `status==pending` and
  `recommendation is None`. Fill `recommendation`/`matrix` on success (status stays `pending`);
  release the run lock on failure (retry later).
- `/fork` / `/fork list` renders pending envelopes (opaque ids, latest per `(slug, category)` with
  supersession) and includes concrete ready-to-run `/fork decide <fork-id> --choice keep|counter`
  commands with the real opaque `fork_id`; `/fork decide <fork-id> --choice keep|counter` is the decide
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

This coupling was removed by the superseding design. The former detector key is now a tombstone and cannot
disable or restore any build protection. The surviving `ace.fork_mpr.enabled` switch controls only the
architecture-fork recommendation worker; framing-note capture and approved-design enforcement are separate.

### D6b — L3 hard-check at the implementation boundary

Pure `ack.ace.constraint_conflict.hardcheck(required, provided, *, require_present)` returns a frozen
`Violation` (`kind=missing|mismatch`) for the first required typed field, or `None`. The always-on engine
wrapper `_design_build_check` deliberately passes only the approved design's normalized `language` field;
omission is a violation when `require_present=True`. Design `network` is never part of this hard-check and
remains solely an explicit egress posture under `## Build policy`.

**Hard floor sites** (IMPLEMENTATION boundary only):

1. **Impl `stage_handover`** (create + re-hand) — PRE-write; task `language` comes from the real TaskSpec.
2. **`plan_units` children** — PRE-write, atomic; same language compare; `force` does not bypass.

**Advisory only:** `record_design` still only emits the L2 fork and records the proposal —
a conflicting design can be recorded but cannot be approved or implemented until consistent.
Couples with the operator decide path: `keep` leaves the floor unchanged and makes `design.md` coherent by restoring
or clearing the rejected counter; `counter` overrides the floor to the design's value and promotes the
counter body (approval proceeds).

### D7 — Reconciled live switch surface

| Historical/runtime name | Config | State | Current role |
|------|--------|---------|------|
| `CONSTRAINT_CONFLICT_DETECT` | `safety.constraint_conflict_detect` | retired tombstone | No live read and no replacement toggle. Legacy values warn and are ignored. |
| `_ACE_FORK_MPR` | `ace.fork_mpr.enabled` | live switch, default `false` | Gates only the optional architecture-fork MPR recommendation/learn worker. It does not gate framing notes or approved-design enforcement. |

The mandatory approved-design implementation check and completion/egress boundaries have no enable rows.

### D8 — Schema surface

New tool parameters remain optional. `record_design` offers the approved-design fields. `record_constraints`
is exposed only when the live `framing_notes.enabled` capture switch is on; it writes non-gating framing
notes. `constraint_gate.enabled` is a one-release alias for that capture switch, not a protection gate.

## Scope map

| Layer | What | Specified in |
|-------|------|-------------|
| L1 (shipped) | Capture, presence gate, verbatim injection | ADR-0015 |
| **Plumbing (this ADR)** | Typed fields, classify, flags | **this ADR** |
| **L2 detect (this ADR)** | Structured conflict → durable `ForkEnvelope` ledger | **this ADR** |
| **L2 surface (this ADR)** | Recommend → decide → learn; `/fork` + `/approve constraint\|design` | **this ADR** |
| **L3 hard-check (this ADR)** | Design/task typed vs HARD typed fail-closed compare at approve + impl | **this ADR** |
| **L2/L3 E2E capstone** | Real-dispatch keep/counter/omission + gate-off byte-identical | **this ADR** |

## Requirements (R1–R6)

- **R1** Pure ACK classifier/normalizer; engine is a thin persist/read layer.
- **R2** Invalid typed input fails closed at capture; missing typed input is not an error.
- **R3** Suggested never enters `_constraint_typed` until promotion.
- **R4 (historical)** The retired detector used strict boolean coercion; it is no longer a live flag.
- **R5** English-only, boundary-clean export; no private literals.
- **R6 (historical)** No silent behavior change when the original optional fields were absent.

## Consequences

- The detector reads HARD typed floors via `_constraint_typed` without re-parsing prose or inventing aliases.
- The detector persists pending fork envelopes; the operator surface fills recommendation/matrix, lists/decides, and learns.
- The L3 hard-check compares design/task typed fields to `_constraint_typed` deterministically; the operator decide
  path (keep vs counter) aligns the floor or the design before the hard
  floor opens.
- The operator surface owns promotion of `suggested → hard` (`/approve constraint`) and the operator command surface.
- `safety.constraint_conflict_detect` is retired and cannot change behavior. `ace.fork_mpr.enabled` affects
  only the optional architecture-fork worker.
- Extending the allow-list later is an explicit contract change (new key + normalize + tests + ADR note).

## References

- ADR-0015 — L1 constraint-aware design gate (capture / presence / injection).
- L2 advisory conflict fork + L3 structured hard-check — the follow-on scope recorded in this ADR.
- Foundation — typed fields + classifier + flags.
- L2 structured detector + durable fork-envelope emission.
- L2 durable project-scoped fork worker + `/fork` surface + decide→learn.
- L3 structured hard-check at the implementation boundary.
