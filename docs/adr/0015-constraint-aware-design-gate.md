# ADR-0015: Constraint-aware design gate

## Status

Accepted — delivered incrementally, default off.

## Context

Design quality depends on constraints that are easy to lose between discovery, design, and implementation:
hard requirements, explicit taboos, and the minimum scope that must survive trade-offs. Free-form conversation
is not a durable authority, while inferring constraints from prose would make provenance and enforcement
non-deterministic.

The design lifecycle therefore needs a small, file-based contract that distinguishes three states: no capture
exists, an operator explicitly declared that no constraints apply, or constraints were captured. It must be safe
to read on every turn, preserve authored constraint text, and leave every existing flow byte-identical unless the
feature is enabled.

## Decision

Adopt a three-part L1 design, delivered through separate increments:

1. **Capture and status (S1 #1338 — shipped).** `record_constraints` writes the single canonical
   `<unit>/decisions/constraints.md` document. The document has closed frontmatter with `type: decision`,
   `stage: constraints`, a consistent `declared_none` boolean, and a title. Empty/whitespace content or the
   case-insensitive `none` sentinel records `CAPTURED_NONE`; otherwise the body is `CAPTURED` and is preserved
   verbatim apart from leading and trailing blank lines. The bounded tri-state reader returns `UNCAPTURED` for
   missing, unreadable, oversized, malformed, inconsistent, or marker-poisoned documents and never raises.
2. **Presence gate (S2 #1339 — shipped).** Before design work proceeds, the lifecycle requires either
   `CAPTURED` or `CAPTURED_NONE`. `UNCAPTURED` blocks `record_design`, `plan_units`, and implementation
   `stage_handover` (create and re-hand). This is a presence decision only; it does not rank, interpret, or
   rewrite constraints. `force` does not bypass. Design/analysis/documentation handovers stay ungated (they
   produce the design).
3. **Verbatim injection (S2 #1339 — shipped).** A captured body is injected as one bounded block delimited by
   the reserved `<!-- IRONCLAD:CONSTRAINTS -->` and `<!-- /IRONCLAD:CONSTRAINTS -->` markers. An explicit
   no-constraints capture injects no body (and strips a stale block). Capture refuses those marker literals in
   authored content, and the reader treats a document containing them as untrusted. Each staging call takes
   **one** status snapshot that drives both the gate and the injection (no TOCTOU); re-hand is idempotent
   (strip-then-add, never accumulate).

The complete surface is opt-in through `constraint_gate.enabled`. While disabled, the tool is not offered,
direct tool dispatch is refused cleanly, and the steering state and existing lifecycle paths are unchanged
(byte-identical default off). Configuration accepts only JSON `true` or the explicit true strings `true`,
`1`, `yes`, and `on`; unrecognized values fail soft to disabled.

## File contract

```markdown
---
type: decision
stage: constraints
declared_none: false
title: Deployment boundaries
---
Constraint body preserved as authored.
```

The reader performs one file read capped at 64 KiB, requires independently verified closing frontmatter, and
then applies the write-side consistency rule: `declared_none: true` requires an empty body, while a captured body
requires `declared_none: false` (or an absent/falsy value). Invalid combinations become `UNCAPTURED`; they are
never repaired implicitly.

## Consequences

- Constraint capture is deterministic, navigable, and auditable without a model call.
- A single canonical document prevents stale sibling records from becoming competing authorities.
- Fail-closed parsing keeps malformed or poisoned state from being treated as a valid lifecycle signal.
- With the gate on, design/decomposition/implementation cannot proceed without an explicit capture
  (including the explicit none-decision); coders always see the authored body when it exists.
- Default-off deployments receive no new prompt text, steering text, model tool, gate, or injection.
- L2/L3 compliance (typed fields, hard/soft provenance, conflict fork, structured hard-check) is specified
  in ADR-0016 / epic #1344; L1 stores and injects the authored body and remains the presence floor those
  layers build on.

