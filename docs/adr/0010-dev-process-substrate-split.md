# ADR-0010 — DEV-2 substrate: the public/private split + the import seam

- **Superseded by [ADR-0011](0011-dev-process-rework-project-isolation.md):** the substrate is relocated back to monorepo-private and the public dev-process surface is the three DEV-1 prompts only. Kept for history; the public/private classification below no longer reflects the shipped boundary.
- **Status:** Accepted — DEV-2 substrate relocation **complete**. **13 standalone
  modules public** under `ack.devprocess`: `ledger`, `lock`, `resume`, `watcher`, `economics`, `abort`,
  `roundtrip`, `worktree`, `credentials`, `dial`, `marker`, `guards` (CI/spec-parity integration tests
  stay private), and `spec` (D2 split — pure model public, the concrete `TARGETS` table stays private
  and re-exports the model). The process-**`coupling`** guards, the epic-**`completion`** trigger, unit
  **`selection`**, and the native composed **`gate`** are public (complete) via the
  intra-package seam (D5); the native **`driver`** state machine and the **`e2e`** harness are public too
  (complete — the e2e split keeps its **DELIVER-leg** ops assembly private, since it imports the
  private `deliver` / `gh release`). The only remaining DEV-2 piece is the runtime guards.
- **Date:** 2026-06-26
- **Context sources:** an analysis of the dev-process work + the decision to keep the engine/devprocess SSOT public
  and the split (driver/guards/substrate public, extension legs private); an evidence-based
  per-module classification of the private dev-loop engine (20 modules); and the
  runtime-context facts (the engine and the reconciler run on
  bare runners with **no** `ack` on `sys.path`).

## Context

DEV-2 ("same discipline, native guards in Ironclad, switchable GitHub push") needs the dev-loop's
**guards + substrate** to live in the public export so the framework — not the orchestrating CLI —
runs them. Today they are private under the dev-loop tree. The work is not "move everything": some
modules are pure, GitHub-agnostic substrate (publishable) while others are GitHub-coupled or carry
private literals (must stay private). The public/private principle was already fixed; this ADR records the **per-module
classification**, the **split mechanism** for the two borderline modules, and the **import seam** by
which the private engine keeps consuming the relocated substrate.

## Decisions

**D1 — Per-module classification (evidence-based).** Each private dev-loop module was read for
(a) any GitHub call (`gh …`), (b) any private literal the boundary check forbids in `core/` (repo
names, host IPs, hostnames, vessel/plugin names, private deploy/config paths), and (c) its I/O and intra-deps.

- **PURE-PUBLIC → `ack/devprocess/` (17):** `abort`, `completion`, `credentials`, `dial`,
  `driver`, `e2e`, `economics`, `guards`, `ledger`, `lock`, `marker`, `resume`, `roundtrip`,
  `selection`, `watcher`, `worktree` — stdlib / filesystem / `git`-only, no `gh`, no private literals.
- **STAY PRIVATE (3):**
  - `deliver` — calls `gh release create` (irreversible GitHub push); an extension leg.
  - `spec` — holds the `TARGETS` table of concrete downstream repo names (incl. the extension plugin
    repo) — private literals.
  - `coupling` — its `_PROTECTED` self-mod class hardcodes this repo's own private paths (the dev-loop
    tree, the CI-guard tree, the workflow definitions) — operationally repo-specific.

**D2 — Split the two borderline modules (`spec`, `coupling`); don't classify them whole.** Their
*pure logic* is public substrate; only their *private data* stays back:
- `spec`: the pure `Spec` model + parsing → public; the `TARGETS` repo table → private (the private
  module imports the public model and supplies its own targets).
- `coupling`: the pure guard functions (`branch_valid`, `code_change_requires_test`, …) → public,
  **parameterized** to take the protected-path set as an argument; the concrete `_PROTECTED` list for
  this repo stays private and is injected.

This unblocks the public-candidate modules that depend on them (`completion`/`selection` → the public
`spec` model; `driver` → the public `coupling` guards). The split lands with those dependents, not in
the first slice.

**D3 — Import seam: public SSOT, private file-load re-export shim (no `import ack`).** The relocated
module is the single source of truth at `ack/devprocess/<m>.py` — shipped in the wheel
(`packages` already includes `ack.devprocess`) and **clean-room tested** via `from
ack.devprocess.<m> import …` (no skip; verifies it ships). The private engine must keep working
**without `ack` installed**: its runner puts only the private dev-loop tree on `sys.path` (bare `import
ledger`) and the reconciler file-loads the private `<m>.py` by path on a bare CI runner. So
the private `<m>.py` becomes a thin **shim** that loads the public module *by file path*
(`parents[2]/ack/devprocess/<m>.py`) and re-exports its public names — pure forwarding, no
`import ack`. This is the file-load lesson applied as the standing seam: one public SSOT, both
consumers (engine + reconciler) unchanged, the clean-room proves the public module independently.

**D4 — Count-neutral test relocation.** Each module's private, skip-if-absent file-load test (under
`ack/tests/`) is rewritten as a public `test_devprocess_<m>.py` importing
`from ack.devprocess.<m>` — same assertions, same monorepo count, but now it **also runs in the
clean-room** (the old one skipped there). Net offline count unchanged; clean-room coverage gained.

**D5 — Intra-package seam (try-import / file-load-sibling); the glue is public in DEV-2.** The D3 shim
works for **standalone** modules. The remaining modules carry intra-`ack.devprocess` dependencies —
`coupling` needs `GuardResult` (from `guards`) + `spec.C0_FORK_LABELS`; `completion`/`selection` need
`spec` labels; `driver` needs `coupling`+`guards`; `e2e` needs `worktree`+`guards`+`driver`. A public
`coupling` doing `from ack.devprocess import guards` crashes on the **bare-runner** consumer
(the reconciler file-loads `coupling.self_mod_protected`) because `ack/__init__` pulls **pydantic**
(the same reason). The seam: a public module imports its siblings with **try-normal-import /
except-file-load-by-path** — in the engine/test context (`ack` importable) it binds the *real* sibling
object (one identity, no duplication); on a bare runner it file-loads the sibling `.py` by path (cached
in `sys.modules`, no `ack/__init__`). `self_mod_protected` is **parameterised** (the caller supplies the
protected-path set); the concrete repo-specific `_PROTECTED` stays private and is injected by the private
`coupling` shim's one-argument wrapper. (Decision history: this was first deferred to the extension tier;
the operator then chose to solve the seam now so DEV-2 gets its defining native enforcement —
`coupling` lands first, the remaining glue `completion`/`selection`/`driver`/`e2e` follows.)

## Consequences

- DEV-2 guards/substrate + the native enforcement coupling become a public, GitHub-agnostic process
  engine the framework owns; only the GitHub-specific legs (`deliver`/`spec`-targets/`coupling`-`_PROTECTED`)
  stay private by design. `completion` + `selection` are public; the remaining glue (`driver`/`e2e`)
  relocates via the same D5 seam.
- The live dev-loop engine and the bare-runner reconciler are untouched (they import the shims); the
  boundary check stays green (no private literals cross into `core/`); the clean-room gains real
  coverage of the relocated substrate.
- **13 standalone modules** relocated by the identical mechanical D3/D4 pattern (`ledger`, `lock`,
  `resume`, `watcher`, `economics`, `abort`, `roundtrip`, `worktree`, `credentials`, `dial`, `marker`,
  `guards`) plus the `spec` D2 split — DEV-2 substrate relocation complete. The orchestration
  glue is deferred to the extension tier (D5).
