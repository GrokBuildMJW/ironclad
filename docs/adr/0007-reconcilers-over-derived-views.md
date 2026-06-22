# ADR-0007 — Reconcilers + invariants over every derived view (process-doctor)

- **Status:** Accepted (design) — implementation under epic #188 (sub-issues #190 backbone, #191 board, #192 issue/label/milestone, #193 doc-lint, #194 mirror/upstream, #195 export-equality, #196 secret-scan, #197 DEV_LOOP-lint, #198 release-version). Generalises [ADR-0006](0006-docs-ia-and-drift-proof-roadmap.md). For what ships see [`status.md`](../status.md).
- **Date:** 2026-06-22
- **Context sources:** the C0 discovery sweep on epic #188 (8 evidence-based investigators over `.github/workflows/*`, `scripts/ci/*`, live `gh` state, the public `ironclad` repo), and ADR-0006 (the first reconciler/invariant, for the roadmap).

## Context

ADR-0006 made **one** derived view (`roadmap.md`) drift-proof: generate it from the source of truth, enforce with an offline lint + a generation check, prune-on-close as a gate. But the same failure **class** is everywhere — a *derived view drifts from reality with no reconciling invariant*, or two event-driven automations race last-writer-wins on a shared field, or a manual step has no gate. The sweep (epic #188) found this across: the **Projects board** (closed-issue card stuck In Progress), **issue/label/milestone metadata** (stale `status/*` on closed issues, orphan epics, undelivered-but-open milestones), **mirror/upstream** state (a `resolved` issue stranded, never `released`), the **public export** (`ironclad`/PyPI is a derived view of `core/` with no equality check), the **secret gate** (runs degraded yet exits 0), and even **DEV_LOOP.md** itself (cites a non-existent workflow; stale version). Patching each as it surfaces is the very "symptom, not structure" trap ADR-0006 named.

## Decisions

**D1 — Every derived view gets the same triple: invariant + on-event guard + scheduled reconciler.**
- **Invariant:** a declarative rule that must always hold (e.g. *closed issue ⇒ board Done*; *closed issue ⇒ no `status/*` label*; *`ironclad@main` == fresh export(core/)*; *resolved public issue ⇒ released on delivery*).
- **On-event guard:** the cheap, immediate enforcement on the triggering event — but it MUST evaluate **re-queried live state**, never the (possibly stale) event payload, or it is itself a partial patch.
- **Scheduled reconciler:** the **load-bearing** healer (cron). It is the only leg immune to Action-run ordering and the only leg that sees **metadata-only mutations** (opening/closing/editing a milestone changes no file, so a path-gated on-push check never fires). Idempotent (write only when current ≠ desired), **fail-closed** (API/auth error ⇒ non-zero, RED, visible — distinguish auth failure (hard-fail) from a transient network blip).

**D2 — `process-doctor` is the executable, repeatable gap-discovery method.** `scripts/ci/process_doctor.py` is a **check registry**: each invariant is a check that asserts against live GitHub state (assert mode) and is reused by the reconciler (heal mode). It runs as a CI job and on demand. This is the durable answer to "how do we find the next gap" — the invariants are codified and continuously asserted, not rediscovered by accident. Every invariant ships with a **negative test** that proves the drift is caught/healed.

**D3 — One scheduled `reconcile.yml`** (cron) runs the heal actions across board / issue-metadata / roadmap / mirror, plus on-event guards live in the relevant workflows. Auth: **reuse existing PATs** — `PROJECTS_TOKEN` for Projects v2 writes, `UPSTREAM_TOKEN` for cross-repo (mirror/release-close), `GITHUB_TOKEN` where same-repo suffices.

**D4 — Coverage of the derived views (Wave 1).** board (#191), issue/label/milestone (#192), roadmap-check-on-real-mutation + the backbone (#190), doc-lint scope (#193), mirror/upstream + liveness (#194), export↔public equality (#195), secret-scan un-degrade (#196), DEV_LOOP self-consistency (#197), release-version invariant incl. #177 (#198). **Deferred (linked follow-ups, not this epic):** deep CI required-checks reconciler + public test-gate alignment (Gap I), and the deploy/spark prod reconciler (Gap K — the Spark is unreachable from CI, so it is an operator-gated check, tracked separately).

**D5 — No "all gaps closed" claim.** The deliverable is: close the found gaps with invariants AND leave `process-doctor` as the standing method so future drift is asserted, not stumbled upon. Coverage and known-not-covered are stated explicitly (the sweep's "not checked" list is carried into the epic).

## Boundary / security

`process_doctor.py` + `reconcile.yml` live in `scripts/ci/` + `.github/` (**private**, never exported). They read/write GitHub metadata only — no secrets in code; PATs come from repo secrets. Reconcilers that touch the public `ironclad` repo do so via `UPSTREAM_TOKEN` and never embed private literals.

## Consequences

- Drift of any covered derived view is **caught and self-heals** within a scheduled cycle, regardless of event ordering — the whole class, not one symptom.
- A new derived view = add one check to `process-doctor` + one heal action to the reconciler + a negative test. Cheap, uniform.
- Some invariants (deploy/spark prod, Gap K) cannot be CI-verified (private LAN) and remain operator-gated — stated honestly, not pretended.

## Alternatives considered

- **On-event guards only (no scheduled reconciler)** — rejected: no number of event handlers eliminates the inter-run race, and none sees metadata-only mutations (D1).
- **A documented sweep checklist instead of `process-doctor`** — rejected: discipline-dependent, the exact trap; the method must be executable + asserted in CI.
- **One mega-fix** — rejected: each derived view is its own sub-issue with its own negative test, so coverage is explicit and verifiable.
