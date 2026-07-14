# Skill packaging & library (design spec)

> **Design/planned** — the contract this targets. What actually works **today** is the typed
> `CASE`+`run` plugin in [`plugin-api.md`](plugin-api.md); the generation engine, the playbook
> kind, and the catalogue are being built under epic #22. See [ADR-0001](adr/0001-skill-engine-and-library.md)
> for the decisions and [`status.md`](status.md) for current wiring status.

Ironclad supports **two skill kinds**. Both are secret-free, English-only, and export-clean.

## Kind 1 — typed tool (`CASE` + `run`) — *available today*

A Python file under a `skills/` dir with a module-level `CASE` and a synchronous `run(...)`.
The typed signature is the tool schema. This is the stable contract in
[`plugin-api.md`](plugin-api.md); nothing here changes it. Scaffold with
`python -m ack.generator`.

## Kind 2 — playbook (`SKILL.md` package) — *available (#89)*

A directory the orchestrator reads with **progressive disclosure** (metadata first → body on
trigger → references on demand). It is loaded by an additive loader path
(`ack.playbook` / `Registry.discover_playbooks`) alongside `discover_skills` (it does not
become a typed tool), and exposed to the model via the **`use_skill`** tool: call it with no
capability to list available skills (metadata only), with a capability to load that skill's
body, and with a reference name to load one reference doc.

```
<skill>/
  SKILL.md          # frontmatter metadata + the routing/instruction body
  references/       # docs loaded lazily, only at the stage that needs them
  scripts/          # optional file-first helpers; a `check` entry point is the validation gate
```

`SKILL.md` frontmatter (the shared metadata schema, below) precedes the markdown body. The
body holds routing rules / instructions; `references/*.md` are pulled in on demand.

## Shared metadata schema

Both kinds carry the same core fields — on the `CASE` dict (tool) or in `SKILL.md`
frontmatter (playbook):

| Field | Required | Meaning |
|---|---|---|
| `capability` | yes | Unique id for the skill (catalogue key). |
| `name` | tool: defaults to `capability` | The tool name the model calls (tool kind). |
| `description` | yes | What it does / when to use it. |
| `kind` | yes | `tool` or `playbook`. |
| `type` | recommended | Taxonomy: `capability` \| `artifact` \| `tool` (from the research Type axis). |
| `domain` | recommended | Taxonomy domain (e.g. `research`, `documents`, `finance`). |
| `trigger` | playbook | Keywords/phrases that activate the skill. |
| `not_for` | optional | Explicit negative scope / hand-off targets. |
| `version` | catalogue | semver; defaults to `0.1.0` for a new skill. |
| `provenance` | catalogue | `built-in` \| `user` \| a source reference (no credentials). |

## Library catalogue (manifest) — *available (#35)*

`ack.catalogue` builds a self-hosted index over both kinds, reading each skill's **own
metadata** as its manifest (the fields above — no separate registry file to drift). It supports
**discover** (`build_catalogue([(root, provenance), …])`), **install** (copy a skill into the
active `skills/` dir), and **update** (replace only when the source has a newer semver), with
**versioning + provenance** and built-in vs user libraries — no mandatory external marketplace.
Install and update fully stage a replacement beside the live skill before atomically swapping it
into place; a copy or swap failure retains the working version — rolled back in place, or (if a swap
AND its restore both fail) preserved in the backup path rather than destroyed. Discovery skips hidden
(`.`-prefixed) directories, so a leftover staging/backup copy never shadows the live skill.
It layers over `Registry.discover_skills` (tools) + `discover_playbooks` (playbooks).

## Generation — *available (#33)*

`spec → deterministic scaffold → bounded LLM body → gate → register`. `ack.skillgen`
(`python -m ack.skillgen --capability … --description … --kind tool|playbook [--param n:t] …`)
scaffolds either kind, contract-correct by construction; the body is a marked stub the author/LLM
fills:
- **Typed tool:** a `.py` with `CASE` + a typed `run(...)` (its signature is the tool schema) +
  an auto-test stub. The richer paved-road *domain* scaffold (Copier-compatible, 3-way merge)
  remains `ack.generator`.
- **Playbook:** a `SKILL.md` + `references/` + `scripts/check` (file-first validation) skeleton.

## Quality gate (before registration) — *available (#34)*

`ack.gate` (`gate(path)` / `gate_tool` / `gate_playbook`) — required for every generated skill:
- **Typed tool:** doctor preflight (loads, `CASE`+`capability`, **sync** `run`, derivable schema)
  **+** an auto-generated test ships alongside it.
- **Playbook:** `SKILL.md` frontmatter-schema validation **+** references readable **+** the
  skill's own `scripts/check` step (exit 0).

**Opt-in (behavioral):** a skill may additionally ship an `eval/` layer
(`gate.toml` thresholds, A/B `harness.py`, `judge.py` panel, `rubric.py`, `sets/`+`refs/`) —
modeled on `skills/mpr/eval/` — run before the merge of a behavior-affecting change, not on
every commit. `skills/mpr` is the reference built-in and the eval exemplar (#90).
