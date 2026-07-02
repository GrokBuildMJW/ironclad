# MPR `eval/` — A/B harness · rubric · judge · eval sets (spec 08)

Home of the MPR plugin's **live/eval layer** (spec 08 §3–§5, §10). Separate from the deterministic unit
tests under `../tests/` (those run model-/net-free in the merge gate); what lives here drives **real** turns
against the deployed orchestrator and costs tokens → it does **not** run on every commit, but before merging
an MPR-relevant change (gate stage 4, spec 08 §7).

## Layout

```
eval/
  README.md            # this document
  harness.py           # A/B harness "MPR on/off" (ctx_harness style, stdlib-only, --selftest)      [Ev-3]
  rubric.py            # rubric as data (pure, unit-tested)                                          [Ev-5]
  judge.py             # LLM judge panel (3 votes, stubbed in the gate; live only in the A/B report) [Ev-5]
  gate.py              # merge-gate evaluation (reads gate.toml, checks thresholds)                  [Ev-6]
  gate.toml            # gate thresholds (coverage_floor/budget/epsilon/decline_rate), tunable       [Ev-6]
  sets/                # curated eval sets per domain (jsonl) — CALIBRATION, needs the user          [Ev-8]
  refs/                # reference-dimension lists per query (ground-truth axes)                     [Ev-8]
  recordings/          # record/replay manifests (test fixtures, generated)                          [Ev-4]
```

All modules (`harness.py`/`rubric.py`/`judge.py`/`gate.py`/`gate.toml`) are **built** (phase-2 units
Ev-3/5/6); the data folders `sets/`/`refs/`/`recordings/` are filled with curated eval data/fixtures
(`.gitkeep` keeps them versionable even when empty).

## Reconcile against spec 08 §1 (IMPORTANT — the spec describes an earlier layout)

Spec 08 §1/§2 were written against the **originally assumed** module split (`mpr.py`, `registry.py`,
`effort.py`, `test_router.py`, the gx10 global fixtures `_StubWorkers`/`restore_flags`). The **built**
phase-1 architecture deliberately differs — the guarantees are identical, the shape is different:

| Spec 08 §1 assumes | Actual (phase 1) | why |
|---|---|---|
| `mpr.py` (CASE+run) | `entry.py` + a thin `skills/mpr_research.py` | the loader scans only `**/skills/*.py`; logic importable+stubbable |
| `registry.py` (flat) | a `registry/` package | consolidated (schema/resolve/synthesis/loader/guards/adaptive/config) |
| `effort.py` | `registry/resolve.py` | effort/policy resolution belongs to the registry |
| `test_router.py` etc. | `test_router_*.py` (8 files) + `test_registry_*.py` … | grown per unit; **one** test root |
| gx10 global fixtures (`_StubWorkers`, `restore_flags`) | **injected deps** (`Deps` dataclass, stubs as arguments) | net-free WITHOUT gx10 on sys.path; cleaner than monolith globals |

The full **§2.1–§2.9 → existing-tests coverage map** lives in the Ev-1+Ev-2 record under
`vault/Plan/mpr/` (private). The structural aggregate gate (`tests/test_eval_coverage.py`) enforces the
invariants (no own dispatcher; the §2 component test files are present).

## P0 dispatch wiring — BUILT (was: deferred)

The "run_mpr → P0 dispatch" wiring is wired **and tested**: `tests/test_p0_dispatch.py` (PW-1) drives the
perspectives through the P0 `ProviderDispatcher` and checks the §2.4 seam (`RouteRequest[]`/`DispatchPolicy`)
and §2.6 provenance net-free via an injected `_StubDispatcher`. This un-defers the previously deferred
spec-08 tests (the former tracking skip-stub no longer exists):

- §2.4 dispatch seam (`_StubDispatcher` captures `RouteRequest[]`/`DispatchPolicy` via `ProviderDispatcher.dispatch`)
- §2.6 provenance from `DispatchResult` (the manifest today carries the in-engine substrate, not dispatch provenance)
- §2.3 envelope/governor (`ReasoningWorkers._plan_concurrency` over a panel)
- §2.7 argv-grep / §2.8 sealed-no-egress (no external argv/egress path in the in-engine MVP)

The **load-bearing** guarantees of these §§ (local-only never external, policy passthrough, effort clamp,
fail-closed) are proven **today** at the **plan seam** (`plan_perspective_dispatch` → `ProviderChoice`) in
`tests/test_sovereignty.py`, deterministically.
