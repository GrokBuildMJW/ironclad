---
capability: dev-process
kind: prompt
description: Plan a change through a disciplined C0→C1→C2 dev loop (readiness, per-unit, completion) with docs-as-code and no-guessing
type: prompt
domain: engineering
languages: [en, de]
variables: [task, change_type]
required: [task, change_type]
ask.task: What is the change / unit of work?
ask.change_type: What kind of change is it (e.g. feature, fix, refactor, docs)?
version: "0.1.0"
provenance: built-in
---
Plan and execute **"{task}"** — a {change_type} change — through a disciplined dev loop. Work the gates in order: do **not** start code before C0 is green, and do **not** call it done before C2 is green.

**C0 — Readiness (before any code):**
- **Design** — name the approach, the affected modules/interfaces/data flows, and the alternative you chose; record real architecture decisions as an ADR.
- **Scope** — every work package exists as a tracked item; dependencies and order named (no "TBD").
- **Runnable target** — define what must run end-to-end after this work, plus the concrete smoke/integration scenario that proves it.
- **Feature coverage** — every touched / new / removed capability is in the target and the doc plan.
- **Test plan** — name the tests per change; the full suite is baseline-green first.
- **Doc & release impact** — list which docs change; if you ship an artifact, plan its release gate.

**C1 — Per unit (one work package at a time):**
- Small, focused, evidence-based; write the tests with the code.
- Keep the docs current in the **same** change (docs-as-code) — leave no drift.
- Green locally (the relevant tests + checks) before you propose the change.
- Review by change type — code gets a real review; a docs-only change rides the doc checks.

**C2 — Completion (before done):**
- All units complete; nothing from C0 left open.
- The C0 end-to-end scenario was **actually run** and is green — not just per-unit tests.
- Coverage met — every C0 capability is implemented, tested **and** documented.
- Tests fresh and green; docs complete and consistent; if you ship, the release gate is green and the published artifact is verified.

**No guessing:** at a genuine architecture fork or an ambiguity this plan does not resolve, **stop and ask** — do not guess.
