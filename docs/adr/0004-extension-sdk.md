# ADR-0004 — Public, versioned Extension SDK

- **Status:** Accepted (design + initial cut) — implementation under epic #132 (sub-issue #72; the internal-loading seam is #136). Records the design; for what ships see [`status.md`](../status.md).
- **Date:** 2026-06-22
- **Context sources:** the loader/registry in `engine/gx10.py` + `ack/{registry,playbook,prompt,promptgen,gate,catalogue,i18n,doctor,generator}.py`, the contract doc [`plugin-api.md`](../plugin-api.md), `pyproject.toml` packaging, and the plan `vault/Plan/self-extending-architecture.md` (open plugin boundary ↔ inbound-closed core). Supersedes the "publish the surface" framing of epic #132's C0 after an evidence check (below).

## Context

Ironclad must be extensible **across a repository boundary**: a separate repo builds a plugin
against a **stable, published** contract (`pip install ironclad-ai`), gets framework updates, and
never forks the core. The core stays **inbound-closed** (no upstream PRs into `core/`); the plugin
boundary is the only open surface (`self-extending-architecture.md` §0/§1.2).

**Evidence check (2026-06-22), correcting C0.** C0 assumed the contract modules were *not*
published. Verified false: `pyproject.toml` ships `packages = ["ack", "ack.lodestar", "ack.devprocess"]`, which
includes **every flat module under `ack/`** (sub-packages such as `ack.lodestar` / `ack.devprocess`
must be listed explicitly — a flat list of `["ack"]` alone would drop them; the clean-room import-smoke
gates this). *(Update, ADR-0011 AD-3: `ack.devprocess` now ships ONLY the curated `ack.devprocess.api`
facade — the dev-process implementation substrate was relocated to monorepo-private `scripts/devprocess/`;
the packages list above is still accurate, just thinner in content.)* A fresh `pip install ironclad-ai==0.0.11` from PyPI
imports `ack.registry`, `ack.playbook`, `ack.prompt`, `ack.promptgen`, `ack.gate`, `ack.catalogue`,
`ack.i18n`, `ack.doctor`, `ack.skillgen`, `ack.generator` — all succeed. So the real gap is **not
distribution** but **curation, an explicit stability boundary, and a documented separate-repo
workflow**: today those modules are reachable but undocumented as API and carry no semver promise.

## Decisions

**D1 — One package, a curated SDK facade (`ack.sdk`).** Keep the single `ironclad-ai`
distribution (no separate `ironclad-sdk` while pre-1.0). Add **`ack.sdk`** as the *one explicit
import surface* a plugin author builds against — it re-exports the stable contract with an
`__all__`. Membership of `ack.sdk.__all__` **is** the public API; everything else under `ack.*` /
`engine.*` is internal and may change. (Considered & rejected for now: a separate `ironclad-sdk`
package — independent semver, but two artifacts/pipelines/docs, premature at 0.0.x; revisit near
1.0. Considered & rejected: re-exporting the whole surface at top-level `ack` — over-broad
commitment, muddies the kernel namespace.)

**D2 — The contract surface.** `ack.sdk` exposes: the **tool kind** (`CASE`+`run` conventions,
`derive_tool_schema`, `Registry`/`Registration`/`RegistrationKind`, `get_registry`, `tool`,
`task_type`); the **playbook kind** (`Playbook`, `parse_playbook`, `discover_playbooks`); the
**prompt kind** (`Prompt`, `Variable`, `parse_prompt`, `discover_prompts`, `assemble`,
`run_prompt`); the **gate** (`gate`/`gate_tool`/`gate_playbook`/`gate_prompt`, `GateResult`);
shared **i18n** (`Localizer`); the self-hosted **catalogue** (`Catalogue`, `SkillEntry`,
`build_catalogue`, `install`, `update`). The CLI **generator** stays `python -m ack.generator`
(documented, not re-exported). Engine internals (`gx10`, `server`, `client`) are **not** SDK.

**D3 — Stability policy (provisional pre-1.0, semver at 1.0).** While `0.0.x`, `ack.sdk` is
**provisional**: additive change expected, any breaking change called out in `CHANGELOG.md`. From
**1.0**, `ack.sdk` follows **semver** with a one-minor-version deprecation window (deprecate →
warn → remove no sooner than the next minor). Plugin authors **pin** the version they build
against and import **only** from `ack.sdk` for a stable contract.

**D4 — Loading is dependency-inverted; the SDK never imports a plugin.** A plugin is discovered,
never imported by `core/`: today additively via `GX10_PLUGINS_DIR` (a dir of `**/skills/*.py`),
and — added in **#136** — via the `ironclad.plugins` **entry-point group** so a *packaged* plugin
(internal or 3rd-party) is found through the contract with zero path/import coupling. The boundary
check + export gates guarantee no concrete plugin (and no private literal) ever enters `core/` or
the public export (proved by the leak-guard test, **#137**).

**D5 — Separate-repo workflow is documented + proven.** `plugin-api.md` documents building a
plugin in its own repo against pinned `ironclad-ai`, validating it with `ack.sdk.gate` before
shipping, and loading it via entry-points or `GX10_PLUGINS_DIR`. C2 proves it end-to-end: a demo
plugin in a separate repo builds against the published SDK and is loaded + run (**#138**).

## Consequences

- A plugin author has **one** import (`ack.sdk`) and a clear stability contract; the kernel
  namespace stays uncluttered and the internal/contract line is explicit and testable.
- No packaging change is required to *distribute* the surface (it already ships); the work is the
  facade, the docs, the policy, and the entry-point seam (#136) — small, low-risk, additive.
- Pre-1.0 provisional status keeps us free to refine the surface while being honest about it.

## Alternatives considered

- **Separate `ironclad-sdk` package** — deferred to ≥1.0 (see D1).
- **Top-level `ack` re-export** — rejected (over-broad, see D1).
- **Dir-only loading (`GX10_PLUGINS_DIR`) without entry-points** — kept for dev, but insufficient
  for a *packaged* private plugin; entry-points (#136) is the robust dependency-inversion seam.
