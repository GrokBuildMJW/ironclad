# ADR-0001 — Skill-generation engine & skill library

- **Status:** Accepted (design) — implementation tracked under epic #22 (sub-issues #89, #33, #34, #35, #90, #88). This ADR records the design; it is **not** a claim that the engine ships yet (see [`status.md`](../status.md) for what actually works now).
- **Date:** 2026-06-21
- **Context sources:** the research catalogue `plan_skill_libary.md` (≈40 Agent-Skill patterns) and Ironclad's real plugin standard `skills/mpr/` + [`plugin-api.md`](../plugin-api.md) + `core/ack` (`Registry`, generator, doctor).

## Context

Roadmap §3 calls for *"auto-generated, **tested** skills + a self-hosted skill library."* Two design inputs exist and they describe **different kinds of skill**:

1. **Ironclad reality (the contract today).** A skill is a Python file under a `skills/` dir exposing a module-level `CASE` dict (`capability` required) and a synchronous `run(...)` whose typed signature *is* the tool schema. `Registry.discover_skills` scans `**/skills/*.py`, registers by `CASE['capability']`, and the engine exposes each as an ACK-validated **agent tool**. `skills/mpr/` is the flagship: a package with `registry/`, `templates/`, and an `eval/` layer (`gate.toml`, `harness.py`, `judge.py`, `rubric.py`, `sets/`+`refs/`+`recordings/`). A typed-tool scaffold generator already exists (`python -m ack.generator`).
2. **Research catalogue (`plan_skill_libary.md`).** A skill is a **markdown "playbook"**: `SKILL.md` (metadata + routing rules) + `references/` (docs loaded lazily) + `scripts/` (file-first helpers/validation), selected by description/trigger match and consumed by the model with **progressive disclosure** (metadata first → body on trigger → references on demand). Taxonomy is by *Type* (Capability/Artifact/Tool) × *Domain*; validation is per-skill script pipelines; outputs are file-first.

These are not interchangeable: kind 1 is a *typed tool the orchestrator calls*; kind 2 is a *playbook the orchestrator reads*. MPR is effectively a kind-1 tool that internally runs a kind-2-style playbook.

## Decisions

**D1 — Support BOTH skill kinds in v1** (operator decision, C0):
- **Typed tool** — `CASE` + `run` (the existing contract; ACK-schema-validated by construction).
- **Playbook** — a `SKILL.md` package (frontmatter metadata + `references/` + optional `scripts/`) with a **second, additive loader path** and progressive-disclosure context loading.

The research patterns are **mapped onto**, not copied into, this model: *Trigger* / *Not-for* / *taxonomy* become metadata fields on both kinds; *progressive disclosure* is native to the playbook kind and, for typed tools, becomes lazy reference-loading inside `run()`; *file-first* stays the artefact convention.

**D2 — Quality gate = doctor preflight + deterministic unit tests required; behavioral `eval/` opt-in** (operator decision, C0).
- Typed tool: must pass the **doctor preflight** (contract/schema valid) **and** its auto-generated unit test(s) before registration.
- Playbook: must pass **SKILL.md frontmatter-schema validation** + **references-exist** + the skill's own `scripts/ … check` step.
- The heavier **mpr-style behavioral `eval/`** (A/B + judge panel + `gate.toml` thresholds) is **opt-in per skill** — for skills whose quality is behavioral, not required for every generated skill.

**D3 — Generation = deterministic scaffold + bounded LLM body, then the gate.** Spec → a deterministic, schema-correct scaffold (typed: via `ack.generator`; playbook: a `SKILL.md`+`references/`+`scripts/` skeleton) → the LLM fills the body → the D2 gate must pass → only then registered. Not free-form LLM generation.

**D4 — Library = manifest-based, self-hosted catalogue over discovery.** Each library directory carries a manifest/index per skill (`capability`, `kind` ∈ {tool, playbook}, `version` semver, `type`, `domain`, `provenance`, `source`); built-in vs user libraries; discover / install / update from your own library; **no mandatory external marketplace** (provenance recorded). The catalogue layers over the existing `discover_skills` (tools) + the new playbook loader.

**D5 — mpr is migrated as the reference built-in** (operator decision A, C0). The generalized format must be a **superset mpr already satisfies**; mpr gains a manifest+semver+provenance and registers through the new catalogue. Hard back-compat: mpr stays byte-identical when gated off and its tests stay green (**381** in the unified core suite). If mpr does not fit the format, the format is wrong (#90).

## Mapping: research pattern → Ironclad realization

| Research pattern (`plan_skill_libary.md`) | Ironclad realization |
|---|---|
| `SKILL.md` + `references/` + `scripts/` | **Playbook kind** package (D1); typed kind stays a `.py` with `CASE`+`run` |
| Metadata: Name / Type / Description / **Trigger** / **Not-for** | Metadata fields on `CASE` (tool) and `SKILL.md` frontmatter (playbook); a shared schema |
| Trigger / description routing + in-file decision table | Tool: model picks the tool by `name`/`description`. Playbook: trigger match → progressive-disclosure body |
| Taxonomy = Type × Domain | `type` + `domain` fields → catalogue facets (D4) |
| Progressive disclosure | Native to playbook loader (metadata→body→references); lazy reference-load in `run()` for tools |
| Per-skill validation pipelines | The D2 gate (doctor + tests for tools; schema + references + `scripts check` for playbooks) |
| file-first outputs | Kept as the artefact/macro convention |
| built-in vs user skills | Catalogue `provenance` + separate library dirs (D4) |

## Boundary / security

Generated and migrated skills live under `skills/` and are held to the same contract as the rest of the public export: **secret-free** (`check_core_boundary.py` literal scan), **English-only**, **export-clean** (the clean-room gate), **schema-validated by construction** (ACK). The catalogue manifest carries no secrets; provenance is a source reference, not a credential.

## Consequences

- A second (playbook) loader path and a catalogue layer are net-new surfaces to maintain alongside `discover_skills` — justified by D1.
- The generator grows a second output mode (playbook) on top of the existing typed scaffold.
- mpr gains a manifest and is exercised as the first catalogue entry, which validates the format on the real flagship (#90).
- No change to the live MPR runtime behavior; back-compat is a hard gate.

## Alternatives considered

- **Typed-tool-only v1** (drop the playbook kind) — tighter scope, but discards the research catalogue's core value (progressive-disclosure playbooks). Rejected by the operator in favor of D1.
- **Mandatory behavioral `eval/` for every skill** — highest bar, but token-costly and slow for every tool skill. Rejected in favor of the tiered D2 gate.
- **Back-compat only for mpr** (no catalogue migration) — leaves the library without its one real skill and doesn't validate the format against reality. Rejected in favor of D5.
