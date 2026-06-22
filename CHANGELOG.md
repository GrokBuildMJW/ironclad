# Changelog

All notable changes to Ironclad are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/); the project is **pre-release** (0.0.x).
Released versions are listed below; upcoming work accumulates under *Unreleased*.

> Docs are code: a change does not ship without a Changelog entry (the promote gate
> enforces a non-empty *Unreleased* section).

## [Unreleased]

## [0.0.15] - 2026-06-22

### Added
- **Release-version invariant + release-aware CHANGELOG gate** (#198, folds #177, epic #188): a release
  is an irreversible publish (ADR-0007). `publish.yml` now runs a **fail-closed preflight before the
  PyPI upload** — the release tag, `pyproject.version` and a non-empty `## [X.Y.Z]` CHANGELOG section
  must all agree (a duplicate version is backstopped by the non-skip upload). `release-close.yml` is
  **gated on publish success** (triggered by the Publish workflow completing `success`) so a reporter's
  issue is never closed "released and available now" before the package is actually live. New
  `scripts/ci/release_preflight.py` carries the pure logic (`release_preflight`, `changelog_release_state`)
  and a `--preflight` CLI that also checks PyPI. **Fixes #177:** promote.sh gate 3 demanded a non-empty
  `[Unreleased]`, which was mutually exclusive with the post-bump state doc-reality-audit demands
  (`pyproject == newest CHANGELOG`); the gate now classifies pending-dev and cut-release as valid and
  only genuine drift as fail, so the whole release flow runs. `test_release_preflight.py` (9).
- **Upstream round-trip + board reconcilers** (#194, epic #188): four invariants on the cross-repo
  and board derived views (ADR-0007). `upstream-closed-is-released` — a public `ironclad` issue that
  reached `resolved` may only be closed via delivery (which stamps `released`); a closed-without-
  `released` issue is drift and is healed (fixed the stranded `ironclad#5`). `open-assigned-in-progress`
  — an open + **assigned** issue must be at least In Progress on the board, healed by adding it and
  setting the column (closes the gap that left the *active epic* invisible while its sub-issues moved).
  `upstream-triaged-has-mirror` + `mirror-wiring-live` (warn) detect a stranded triage and a rotted
  intake. `mirror-from-public.yml` is hardened to **create the mirror before** labelling `triaged`
  (a partial failure can no longer strand a report), and `reconcile.yml` routes the upstream heals via
  `UPSTREAM_TOKEN` and board heals via `PROJECTS_TOKEN`. `test_process_doctor.py` (+7). 
- **Secret scan un-degraded** (#196, epic #188): the export secret-gate could pass **degraded** when
  gitleaks was absent (fail-open). `export_core.py --require-scanner` (+ `EXPORT_REQUIRE_SCANNER=1`)
  now **fail-closes** if no scanner ran; a new private CI `secret-scan` job installs gitleaks and
  exports with that flag, and the **public** `ironclad` CI gains a `gitleaks` job so a secret pushed
  straight to the public repo is caught too (the private gate can't see a public hand-push).
  `test_export_secret_gate.py` (2). Per ADR-0007.
- **Export↔public byte-equality check** (#195, epic #188): `scripts/ci/export_sync_check.py` +
  `export-sync-check.yml` (scheduled + on core/ push) assert the public `ironclad` repo is a faithful
  export of `core/` — LF-normalised (a CRLF-only diff is never drift). A file present in public that
  the export does not produce (a hand-edit) is always drift; when public's `.export-source` stamp ==
  HEAD the trees must be byte-identical. `publish_core.sh` now writes that source stamp on push so
  drift is distinguishable from the normal "main ahead of the published release" state.
  `test_export_sync_check.py` (5). Per ADR-0007.
- **DEV_LOOP self-consistency lint** (#197, epic #188): the binding control prompt is itself a
  derived view — process-doctor `devloop-self-consistent` asserts every `*.yml` it cites resolves to
  a real file and every `aktuell v<X>` equals the pyproject version. Fixed the live drift it caught:
  the ghost `mirror-to-dev.yml` ref (real one is `mirror-from-public.yml`), the stale `v0.0.7`
  pointers (→ v0.0.14), and the drifty hard-coded test counts (→ point to test-report.md).
  `test_process_doctor.py` (+3 = 15). Per ADR-0007.
- **Doc-lint coverage** (#193, epic #188): a process-doctor warn `open-milestone-has-description`
  (an open milestone with work but no description is invisible on the generated roadmap); the release
  gate (`promote.sh`) now runs `gen_roadmap.py --check` so a release can never ship a stale roadmap.
  docs-guide records the enforcement. `test_process_doctor.py` (+1 = 12). Per ADR-0007.
- **Issue/milestone invariants in process-doctor** (#192, epic #188): `delivered-milestone-closed`
  (a milestone with work done + 0 open issues must be closed so the generated roadmap drops it —
  assert + heal) and `open-epic-has-milestone` (an open `type/feature` epic with no milestone is
  invisible to the roadmap — **warn**, surfaced for operator triage; picking the milestone is a
  product call). Adds a `warn` tier to the check framework. `test_process_doctor.py` (+3 = 11).
  (Live: #75 surfaced for milestone triage.) Per ADR-0007.
- **Board invariant: closed issue ⇒ board Done** (#191, epic #188): the Projects-board card race
  (#176) is now structurally closed. `project-status.yml` gains a **closed-guard** that re-queries
  **live** issue state (not the event payload) and refuses to set In Progress on a closed issue;
  process-doctor gains a `board-closed-is-done` check (assert + heal) so the **scheduled** reconciler
  is the load-bearing healer that returns any stuck card to Done regardless of Action-run ordering.
  `test_process_doctor.py` (+3 = 8). Per ADR-0007.
- **process-doctor + scheduled reconciler backbone** (#190, epic #188): `scripts/ci/process_doctor.py`
  — an executable check registry that asserts the derived-view invariants against live GitHub state
  (`--check`) and heals them idempotently (`--reconcile`), fail-closed (a gh **auth** failure
  hard-fails, never soft-skips). Seed invariant: *a closed issue carries no `status/*` label* (assert
  + heal; the 12 live stale labels were reconciled). A scheduled `.github/workflows/reconcile.yml`
  runs the healer daily — the load-bearing leg that catches metadata-only mutations (Gap C: a
  milestone/label change triggers no path-gated push check). `gen_roadmap.py --check` now hard-fails
  on a gh auth failure (distinct from a network soft-skip). `test_process_doctor.py` (5). Per ADR-0007.
- **ADR-0007 — reconcilers + invariants over every derived view** (#189, epic #188): generalises
  ADR-0006 from the roadmap to the whole "derived view drifts from reality" class (board, issue/label/
  milestone metadata, mirror/upstream, public export, secret gate, DEV_LOOP). Each view gets an
  invariant + on-event guard (re-queried live state) + a **scheduled reconciler** (the load-bearing
  healer; idempotent, fail-closed), all asserted by an executable `process-doctor` with a negative
  test per invariant. Records the Wave-1 scope + the deferred follow-ups (deep CI required-checks,
  deploy/spark prod).

## [0.0.14] - 2026-06-22

### Changed
- **Roadmap rule: open-milestone (not open-epic)** (#185, epic #169): the generated roadmap now
  renders one section per **open milestone** (its description = the phase narrative); a delivered
  phase drops by **closing its milestone** (M3 + M6 closed). This refines #176's initial
  "milestone with ≥1 open epic" rule, which would have erased an active phase the moment its current
  epics merged (e.g. M5 vanishing when epic #169 itself closed). ADR-0006 D2/D3, `docs-guide.md`,
  and the DEV_LOOP/NEW_EPIC C2 prune wording updated to match.

### Added
- **Generated, drift-proof roadmap** (#176, epic #169): `roadmap.md` is now produced by
  `scripts/ci/gen_roadmap.py` from the **open roadmap phases** (open milestones with ≥1 open
  `type/feature` epic) — a phase drops off automatically once its epics close, so realized work can
  never linger (the structural fix). The per-phase narrative lives in the **milestone description**
  (the single editable source); the generator only renders. A CI job (`roadmap-generated`)
  regenerates and fails on drift (soft-skips if the GitHub API is unavailable; the offline per-doc
  lint is the always-on guard). Pure `render_roadmap` is unit-tested; `test_gen_roadmap.py` (5).
  Per ADR-0006.
- **doc-reality-audit: per-doc responsibility lint** (#174, epic #169): a new fail-closed check —
  `roadmap.md` must contain no realized markers (`shipped`/`delivered`/`wired + tested`/`now|generally
  available`) and `status.md` no future markers (`coming soon`/`will ship`). File-scoped + tiny
  marker lists so legitimate usage (status.md's own `wired + tested`, `see the roadmap` pointers) is
  never flagged. Covered by `test_doc_audit.py` (9) incl. the **negative test** (a deliberately
  "realized" roadmap item makes the audit FAIL) + a real-docs-pass regression. Per ADR-0006.

### Changed
- **README reconciled to the doc IA** (#173, epic #169): the `## Roadmap` section is now a clean
  pointer (planned → roadmap.md, runs-now → status.md) instead of a duplicated, drift-prone
  "done vs planned" list — fixing the stale claim that roadmap.md shows "what works today" and the
  outdated "Phase g" reference. README stays intro/quickstart/value-prop; the per-component wiring
  matrix lives only in status.md (no overlap). (Repo hygiene: the untracked root-level duplicate of
  `vault/Plan/plan_skill_libary.md` — a byte-identical local stray — was removed; the tracked copy
  is the canonical one.)
- **roadmap.md is now future-only** (#172, epic #169): removed the realized content that violated the
  "forward-looking only" contract — the delivered skill-generation engine (ADR-0001/0002), the
  prompt library + discovery/invocation (ADR-0003/0005), and the shipped Extension SDK (ADR-0004),
  plus the two fully-closed phases (skill-generation, prompt-library usability). That content lives in
  `status.md` (wiring SSOT) + `CHANGELOG` history. Roadmap now carries only open themes (enterprise,
  connectors, broader model/data, release maturity). Deferred-but-unscoped items (skill/prompt
  curation, save-as-item) have no open epic yet → tracked in [ADR-0005](docs/adr/0005-prompt-skill-discovery-invocation.md), will reappear on the roadmap when epic'd. Per ADR-0006.

### Added
- **Documentation guide (`docs/docs-guide.md`)** (#171, epic #169): a contributor-facing
  "where does this go?" reference — one responsibility per doc (README=intro, status=now/SSOT,
  roadmap=future-only/generated, CHANGELOG=history, ADRs=decisions), a decision guide, and the
  enforcement summary. Linked from README; README's roadmap pointer corrected ("what works today vs
  planned" → "planned or in progress, future only"). Per [ADR-0006](docs/adr/0006-docs-ia-and-drift-proof-roadmap.md).
- **ADR-0006 — documentation IA + drift-proof generated roadmap** (#170, epic #169): records the
  root cause of recurring doc drift (the audit has no notion of "realized"; prune-on-close is a
  manual, gateless step) and the fix — one responsibility per doc (README=intro, status=now/SSOT,
  roadmap=future-only, CHANGELOG=history), a **generated** roadmap (from open top-level epics, so a
  closed epic auto-drops), prune-on-close as a C2 gate, and a two-layer audit (offline
  forward-only/per-doc lint + a generation check). Implementation follows in #171–#176.

## [0.0.13] - 2026-06-22

### Added
- **ADR-0005 + roadmap phase 6** (#161, epic #146): records the discovery + per-item invocation +
  `/catalogue` design as [ADR-0005](docs/adr/0005-prompt-skill-discovery-invocation.md) (closing the
  [ADR-0003](docs/adr/0003-prompt-library.md) D5 gap), and adds roadmap **phase 6** "Skill & prompt
  library — usability & content" (the shipped usability/seed foundation + the forward-looking
  curation/maintenance flow). roadmap.md phase 3 marked delivered.
- **Three curated prompt-library seeds** (#150, epic #146): `pr-description`, `refactor-plan`, and
  `test-plan` (engineering, EN+DE) bring the curated starter set under `skills/prompts/` to
  **7**. Each is one declarative `kind: prompt` MD file (+ a `locales/de.json` overlay), passes
  `ack.gate`, and is visible via `/prompts` + invocable via `/<name>` purely by dropping the file —
  no engine change. `test_prompt_library.py` now parametrises over all 7 (24).
- **Catalogue endpoint + dynamic slash autocomplete** (#149, epic #146): a guarded `GET /catalogue`
  serves the loaded registry snapshot (the same `_catalogue_snapshot` that backs `/prompts`/`/skills`
  — one surface). It is gated like `/tasks`/`/doctor` (added to `GATED_PATHS`; 401 without the
  deployment secret under token/sealed). The TypeScript client fetches it lazily on the first
  slash-menu open and merges loaded **prompt** names into autocomplete as directly-invocable
  `/<name>` entries — a built-in command wins on a name collision; skills (not bare-slash invocable)
  are intentionally not injected. Fail-soft: an older server / gated session → built-in commands
  only. `test_catalogue_endpoint.py` (3) + `catalogue.test.ts` (6).
- **Per-item prompt invocation `/<prompt-name>`** (#148, epic #146): the command router resolves a
  `/<prompt-name>` against the loaded prompt catalogue and runs it deterministically (model-free) —
  parses `var=value` / a single positional / `--lang xx`, and reuses the `ack.promptgen` elicitation
  state machine: assembles the finished prompt in the target language when all required variables are
  present, else returns the guiding questions for what is missing. Resolution runs **after** every
  built-in command, so a real command is never shadowed; an unknown `/x` still falls through to a
  model turn. The model-elected `use_prompt` tool stays available (additive). A single positional value fills the
  lone required variable verbatim (a `=`/`--lang` inside a code/diff value is preserved), with a
  trailing `--lang xx` peeled; explicit `var=value` sets named variables. `test_prompt_invocation.py` (14).
- **Discovery commands `/prompts` + `/skills`** (#147, epic #146): read-only listing of the **one
  loaded registry** (no re-scan, no parallel mechanism). `/prompts` lists every loaded
  `kind: prompt` item (name, declared languages, description); `/skills` lists every loaded skill
  across both kinds — `SKILL.md` playbooks and typed `CASE`+`run` tools (incl. the MPR built-in).
  Both are advertised in `/help` and offered in the TypeScript client's slash autocomplete. Backed
  by a shared `_catalogue_snapshot` helper over `_PROMPTS`/`_PLAYBOOKS`/`_PLUGIN_TOOLS`.
  `test_discovery_cmds.py` (6) + `classify.test.ts`.

## [0.0.12] - 2026-06-22

### Changed
- **Internal DEV→Prod→Public promote finalized** (#40, epic #132): the release pipeline now runs as
  a single fail-closed gated flow — boundary → **full** test suite (incl. the MPR built-in, which
  the previous step missed) → docs gate (CHANGELOG + doc-reality-audit) → export gates →
  **PRE-publish clean-room** (wheel → fresh venv → import-smoke incl. `ack.sdk` → an example plugin
  builds against the installed SDK) → review → publish → prod redeploy; dry by default. Full
  automation (scheduled sync) deferred. (Core-maintainer machinery; downstream users never touch it.)

### Added
- **Example plugin + SDK clean-room guarantee** (#138, epic #132): a standalone example plugin
  (`examples/example-plugin/`) shows the separate-repo authoring shape — a package with a `skills/`
  dir + an `ironclad.plugins` entry point, built against `ironclad-ai`. The clean-room now
  import-smokes `ack.sdk` and **builds the example against the freshly-installed wheel**, asserting
  it registers its entry point, runs, and matches the SDK schema — proving a separate repo can build
  against the published artifact. 3 tests (skip in installed trees; the workflow covers that path).
- **Export-leak guard for internal plugins** (#137, epic #132): the boundary check + export
  secret-sweep now forbid the internal plugin repo name, and a leak-guard test pins the guarantee
  (the guards flag a synthetic leak; the real `core/` + `clients/ink` tree is clean). `core/`
  couples to plugins only via the generic `ironclad.plugins` entry-point group — never a concrete
  private plugin. (The guards live in `scripts/ci/`, private; the test skips in installed/clean-room
  trees where they're absent.) 4 tests.
- **Packaged-plugin loading via entry points** (#136, epic #132): a pip-installed plugin
  (3rd-party or internal) is discovered at startup through the `ironclad.plugins` **entry-point
  group** — additively alongside built-ins + `GX10_PLUGINS_DIR`, with no path config and no core
  change. Dependency-inverted: the engine resolves each entry point to a plugins dir
  (package / callable / path) and scans it; it **never imports a concrete plugin**. Broken entry
  points are fail-soft. [ADR-0004](docs/adr/0004-extension-sdk.md), `plugin-api.md`. 10 tests.
- **Extension SDK (`ack.sdk`)** (#72, epic #132): a curated, versioned import surface to build a
  plugin in a **separate repo** against `pip install ironclad-ai` — re-exports the tool/playbook/
  prompt kinds, the registration/eval `gate`, `derive_tool_schema`, `Localizer`, and the
  `catalogue`. `ack.sdk.__all__` **is** the public API; everything else under `ack.*`/`engine.*`
  is internal. Provisional while `0.0.x`, semver from 1.0. [ADR-0004](docs/adr/0004-extension-sdk.md)
  + `plugin-api.md` (separate-repo workflow). 7 tests. *(The contract modules already shipped in
  the `ironclad-ai` wheel; this adds the curated surface, the stability policy, and the docs — no
  distribution change. The packaged-plugin `ironclad.plugins` entry-point seam is in development, #136.)*

## [0.0.11] - 2026-06-21

### Added
- **Curated multilingual starter prompt library + eval gate** (#111): four built-in `kind: prompt`
  items ship under `skills/prompts/` — `code-review`, `commit-message`, `bug-report`,
  `explain-code` (EN + DE, each loaded at startup and offered via `use_prompt`). New
  `ack.gate.gate_prompt` is the registration/eval gate for prompt items (required vars must appear
  in the template; every declared language must assemble cleanly; present locale overlays validated)
  and `ack.gate.gate()` auto-routes `kind: prompt` SKILL.md here. `discover_playbooks` now skips
  prompt items cleanly. **A new prompt = drop an MD file, no engine change.** 20 tests.
- **Prompt slash surface & guided elicitation** (`use_prompt`, #110): a discovered `kind: prompt`
  item is exposed as an engine tool — call with no capability to **list**, or with a capability +
  a `values` JSON of what's collected so far to drive **guided elicitation** (the tool returns the
  next missing required variable's question, one at a time) and, once complete, the **assembled**
  prompt in the target `lang` (preview). Wired through `_load_skills`/`_effective_tools`/dispatch;
  the state machine is `ack.promptgen.run_prompt` (deterministic, LLM-free). 9 tests.
- **Multilingual prompt assembly** (`ack.promptgen`, #109): `assemble(prompt, values, lang)`
  renders a `kind: prompt` template + collected values into a finished prompt in a target
  language via `ack.i18n` (per-item `locales/`, source/target + fallback); `missing_required()`
  drives the elicitation loop. Deterministic, LLM-free. 6 tests.
- **Prompt-library item format** (`ack.prompt`, `kind: prompt`, #108): parse/validate/discover a
  declarative prompt item (variables + languages + per-variable elicitation), reusing the shared
  `ack.playbook` frontmatter parser (one parser, no parallel infra). A prompt is a core built-in,
  distinct from `kind: playbook`. 7 tests.
- **Design: prompt library & generator** ([ADR-0003](docs/adr/0003-prompt-library.md) +
  [`prompt-packaging.md`](docs/prompt-packaging.md), epic #105) — a curated, multilingual prompt
  library on the core base: a prompt is a declarative `kind: prompt` core built-in (variables +
  languages + guided elicitation), reusing `ack.playbook`/`ack.catalogue`/`ack.gate`/`ack.i18n`;
  `/<prompt-name>` → elicit → multilingual assembly → preview. Design only — built under epic #105.

## [0.0.10] - 2026-06-21

### Changed
- **Export/deploy/docs aligned to core MPR** (#116): `export_core.py` drops the separate MPR
  bundling (MPR ships via the core/ copy + is covered by the core boundary check); the install
  launchers no longer set `GX10_MPR`; the installer no longer copies a separate `skills/mpr`; the
  MPR README documents the single runtime `mpr.enabled` gate. Finalizes the `GX10_MPR` deprecation.
- **MPR is now a core, always-on built-in** (#115): moved `skills/mpr` → `skills/mpr`;
  removed the `GX10_MPR` load gate (MPR is always loaded) — the live on/off is the runtime
  config **`mpr.enabled` (default ON)**. MPR consumes the core registry + `ack.i18n` + catalogue
  + gate. Back-compatible (behavior unchanged when enabled); its suite (381) is now part of the
  core `pytest` run, so the private CI gates it too. **Deprecation:** `GX10_MPR` is gone — use
  `mpr.enabled` (or `GX10_MPR_ENABLED`) instead.

### Added
- **Always-on core built-in loader** (#114): built-in skills/prompts load at startup from a
  fixed core dir (`skills/`), scanned **unconditionally** — independent of
  `GX10_PLUGINS_DIR`, which stays the **additive** surface for 3rd-party/user skills
  (`_load_skills`). Built-ins now work out of the box with no config. 4 tests.
- **Shared content i18n `ack.i18n`** (#107): the file-overlay locale loader is promoted to core as
  `Localizer(locales_dir)` — flag-independent (always importable, no `GX10_MPR`/plugin coupling),
  parameterized locales dir, English fallback. MPR migrated onto it (`skills/mpr/i18n.py` is now a
  thin shim; 382 tests green, behavior unchanged). Distinct from `engine/messages.py` (engine
  chrome). 6 tests. Part of the core-always-on rebuild (ADR-0002).
- **Design: skill/prompt/MPR as core always-on** ([ADR-0002](docs/adr/0002-core-always-on-skills.md),
  epic #112) — built-ins load from a fixed core dir independent of `GX10_PLUGINS_DIR`; the plugin
  surface stays for 3rd-party skills; MPR de-plugined into core (runtime `mpr.enabled`, default on,
  replacing the `GX10_MPR` boot flag). Design only — implemented under epic #112.

## [0.0.9] - 2026-06-21

### Added
- **Skill lifecycle verified end-to-end** (#88): a model-free integration test drives the full
  pipeline for both kinds — `ack.skillgen` generate → `ack.gate` registration gate →
  `ack.catalogue` install/register → engine load (`_load_plugins`/`_load_playbooks`) → invoke
  (a typed tool returns a real result; a playbook loads via `use_skill`). This is the epic #22
  C2 runnable scenario. 3 tests.

### Changed
- **`mpr` migrated as the reference built-in** (#90): its `CASE` now carries the catalogue
  manifest fields (`type`/`version`/`provenance`), so `ack.catalogue` indexes it as a built-in
  skill — proving the generalized format is a superset of the real flagship. Additive +
  back-compatible (byte-identical when gated off; mpr suite 382 green).

### Added
- **Skill registration gate** (`ack.gate`, #34): no unchecked skill enters the toolset. A tool
  must pass a doctor preflight (loadable, `CASE`+`capability`, synchronous `run`, derivable tool
  schema) and ship an auto-generated test; a playbook must have valid `SKILL.md` frontmatter +
  readable references + a passing `scripts/check`. Behavioral `eval/` stays opt-in. 7 tests. Also
  made the scaffolded playbook `scripts/check` self-contained (no import path assumptions).
- **Skill library catalogue** (`ack.catalogue`, #35): a self-hosted, versioned index over both
  skill kinds, reading each skill's own metadata as its manifest (`capability`/`kind`/`version`/
  `type`/`domain`/`provenance`/`source`). Discover, install (copy into the active `skills/`),
  and update-when-newer (semver), with provenance and built-in vs user libraries — no external
  marketplace. Zero new deps; 6 tests.
- **Skill generator** (`ack.skillgen`, #33): `spec → schema-valid scaffold` for both skill
  kinds — a typed `CASE`+`run` `.py` (signature → tool schema) with an auto-test stub, or a
  `SKILL.md` playbook package + `references/` + `scripts/check`. Contract-correct by
  construction; the body is a marked stub for an author/LLM to fill. CLI `python -m ack.skillgen`.
  Zero new deps; 7 tests.
- **Playbook skill kind** (`SKILL.md` packages, ADR-0001 / #89): a second skill kind alongside
  the typed `CASE`+`run` tool. `ack.playbook` parses + validates `SKILL.md` frontmatter and
  `Registry.discover_playbooks` discovers packages; the engine exposes them via the new
  **`use_skill`** tool with **progressive disclosure** (list metadata → load body → load a
  reference on demand). Zero new dependencies; 15 deterministic tests (`test_playbook.py`).
- **Skill-engine design**: [ADR-0001](docs/adr/0001-skill-engine-and-library.md) +
  [`skill-packaging.md`](docs/skill-packaging.md) — the design for the skill-generation engine
  & self-hosted library (two skill kinds: typed `CASE`+`run` tools and `SKILL.md` playbooks;
  doctor+tests registration gate with opt-in behavioral eval; manifest catalogue with semver +
  provenance; `skills/mpr` as the reference built-in). Design only — built under epic #22.

## [0.0.8] - 2026-06-21

### Added
- **Public-release clean-room gate** (`.github/workflows/clean-room.yml`, #58). Before any
  publish, the package is proven installable + runnable **from the published sources in
  isolation**: PRE-publish builds the wheel, installs it into a fresh venv (not `-e`),
  import-smokes from a neutral dir and resolves the `[engine]`/`[memory]` extras, and does a
  fresh `clients/ink` build + test; POST-publish installs the real package from PyPI and
  import-smokes. Catches forgotten dependencies/files before a user hits them.

### Fixed
- **Published doc link** (`engine/README.md` → the bundled `clients/ink/` client) now
  resolves in the released tree; the export now normalizes the `../` depth of links to the
  bundled siblings (`clients/ink`, `skills/mpr`), and a deterministic doc-reality audit gate
  guards against dead links/anchors, version drift, and stale claims going forward (#41).

### Changed
- **Docs: release-status & test counts reconciled to reality** (#59). The status/honesty
  sections (`README.md`, `docs/status.md`, `docs/roadmap.md`, `SETUP.md`) now state the
  actual release model — pre-release `0.0.x` alpha, tagged releases on PyPI (`ironclad-ai`)
  **and** GitHub Releases (`v0.0.7`), no stable 1.0 — instead of the stale "no tagged
  release". Test counts regenerated from `pytest --collect-only`: **468** Python
  (459 offline + 9 live) and **337** TypeScript client tests; `docs/test-report.md` area
  table updated to match.
- **English-only hygiene**: translated the remaining German code comments in the bundled
  test suite (`ack/tests/test_autoplan.py`, `test_client_pool.py`, `test_workers.py`) to
  English (#78). Deliberate `language=de` user-facing output and the German query-classifier
  keywords are intentionally kept.

## [0.0.7] - 2026-06-21

### Changed
- **Installer is type-aware + config-driven.** The `ironclad`/`ironclad-doctor` launchers now support both a
  local engine (`type: desktop`, default) and a thin client against a remote orchestrator (`type: spark`),
  and read optional tuning (`engineConfig`, `warmUrl`, `claudeBin`, `fanoutConcurrency`, `workersMaxTokens`,
  `workersMaxBatchTokens`) from the project config — absent → engine defaults, so a deployment can tune
  without editing scripts. Adds `install/ironclad-commands.ps1` (profile shim). All still secret-free.

## [0.0.6] - 2026-06-21

### Added
- **One-shot desktop installer** (`install/`, cross-platform, secret-free). Run `install/ironclad-install.sh`
  (Linux/macOS) or `install\ironclad-install.ps1` (Windows) once from a clone to build a venv, install the
  engine, build the optional TypeScript client, write a per-project config and wire an `ironclad` command —
  plus `ironclad` (launcher: engine-ensure + client, version-aware) and `ironclad-doctor` (status). All
  endpoints default to localhost and are overridable via flags / `GX10_*` env; nothing about a deployment is
  baked in. See [`install/README.md`](install/README.md), the *One-shot install* block in
  [`SETUP.md`](SETUP.md), and **Track D** in [`AGENTS.md`](AGENTS.md) (AI-agent install runbook).

### Changed
- The boundary check (`scripts/ci/check_core_boundary.py`) now also literal-scans `.ps1`/`.sh`, so the
  installers are held to the same secret-free contract as the rest of `core/`.

## [0.0.5] - 2026-06-21

### Changed
- **MPR is multilingual.** The deterministic report rendering (decision-matrix / comparison-matrix /
  risk-register / evidence-report templates), the synthesis prompts and the degrade/panel messages are
  now localized: **English is the source**, German ships as a locale overlay (`skills/mpr/locales/`),
  and the render language follows the configured output language. Adding a language is a data file, no
  code change.
- **Orchestrator system prompt hardened.** It now self-invokes plugin tools when a request matches a
  loaded tool (instead of telling the operator to type commands), never guesses initiative names from
  memory (the active initiative comes from state), and never invents command syntax.

### Fixed
- Python 3.10 compatibility for the bundled MPR plugin: its eval gate read TOML via the stdlib
  `tomllib` (3.11+ only). It now falls back to the `tomli` backport (declared as an `[engine]`
  dependency on `python_version < "3.11"`), so MPR loads and its tests pass on 3.10.
- **MPR decision matrix self-consistency:** the recommendation rationale no longer restates invented
  weighted sums — only the MPR-computed score is shown; the fallback line no longer doubles the trigger
  conjunction or the terminal period.
- **MPR conflict detection** no longer emits noise zones from query meta-vocabulary (question words,
  structural terms, criteria names) nor a fabricated top-recommendation conflict; inferred subjects are
  anchored to the question and de-duplicated.
- **MPR run indexing** in a reasoning-only initiative no longer attempts (and silently fails) a TaskStore
  write — consistent with the initiative type contract; the run's record is its manifest + the vault index.
- **MPR TaskStore binding** in the engine glue is fixed (it bound a function object, never an instance), so
  runs in a software initiative are indexed correctly.
- **CLI slash-command menu:** Tab and Enter accept the highlighted suggestion (a single match too), and an
  arrow key accepts a lone suggestion; the renderer's focus manager no longer swallows Tab.
- **CLI:** the MPR report's machine sentinels (`<<<MPR_REPORT>>>` / `<<<END>>>`) are stripped from the
  rendered chat.

## [0.0.4] - 2026-06-20

### Added
- **Initiative-centric state layout.** Engine machinery moves out of the project root into a hidden
  `state_root` (`.ironclad/`: `session.json`, the local warm-cache, the `active` marker) and every
  produced artifact lives under the **active initiative** `vault/<slug>/` — visible `decisions/`,
  `proposals/`, `reviews/`, `runs/`, `tasks/`; hidden `.work/` machine plumbing (active handover,
  handover/feedback inbox, archive). Initiative are created explicitly
  (`/initiative new|list|use|active|reconcile`, `--type mpr|software`); the `TaskStore`,
  `stage_handover`/`advance_pipeline`, reviews and the MPR `runs_dir` all route to the active
  initiative, **fail-closed** when none is active (no writes into the project root), while background
  scanners soft-skip. The local code-agent scratch moves to a hidden `.ironclad/agent/` drop zone.
  Overridable via `paths.state_root` / `paths.vault_root`. See [`docs/state-and-initiative.md`](docs/state-and-initiative.md).
- **Self-maintaining vault** (`reconcile_vault`, `/initiative reconcile`): deterministic, **LLM-free**
  upkeep — a regenerated `INDEX.md` (grouped, Obsidian `[[links]]`, manual prose preserved outside the
  AUTO block) plus an idempotent "Verwandt (auto)" relation block injected into curated docs
  (shared frontmatter tags / title reference). Auto-fires index-only after a write (initiative create,
  `stage_handover`, `advance_pipeline`, an MPR run); the full link pass runs on the explicit command.
- **MPR multi-perspective reasoner** (`skills/mpr/`) now ships in the OSS as the flagship plugin
  example: an expert role-panel router → governed fan-out → deterministic synthesis (decision-matrix /
  comparison / risk / evidence templates) with a sovereignty/budget-gated audit trail. Loaded via the
  open plugin surface (`GX10_PLUGINS_DIR`), runtime-gated by `mpr.enabled` (default off), used through a
  `--type mpr` initiative. See [`skills/mpr/README.md`](skills/mpr/README.md).
- **Operator security guide** ([`docs/security.md`](docs/security.md)): the trust profiles
  (`open`/`token`/`sealed`), their config keys + env overrides, the gated routes, the session
  lifecycle, and the client header contract — in one place.

### Changed
- `session.json` now lives under `.ironclad/` (was `.gx10_session.json` in the project root).
- Default workspace no longer scatters `tasks/`/`summaries/`/`reviews/` into the project root.
- **Warm tier wired by default** — the shipped `docker-compose` now sets `GX10_WARM_URL` to the
  `mem-valkey` loopback, so the warm tier (rolling summary + retrieval cache) activates automatically
  under `--profile memory` (fail-soft otherwise); previously the container shipped but was never wired.
- **`security.profile` is now a boot-only (frozen) config key** — `/config set security.profile …` is
  refused (it wires the trust policy + bind host once at boot); set it in the deploy and restart.
- **Plugin tool names must be unique** — a name clash now keeps the first-loaded tool and warns,
  instead of silently shadowing it.
- **Docs reconciled against the code** (a 10-dimension docs↔code audit): `status.md`, `setup-types.md`,
  `config-runtime.md`, `plugin-api.md`, `code-agents.md`, README/SETUP/AGENTS — corrected over-claims
  (the boundary check does not sandbox plugins), completed the env/config surface, added the
  `/initiative new` onboarding prerequisite, and refreshed the test counts.

## [0.0.3] - 2026-06-19

### Added
- **Scalable-context memory** (multi-tier, default-on when a memory/warm service is configured):
  a **warm tier** (BSD-licensed in-memory store) holding the rolling conversation summary,
  recent-turn state, and a short-TTL retrieval cache that survives a server restart and is shared
  across the reasoning workers; **rolling/hierarchical summarization** on eviction (raw archived
  to long-term, prefix-stable); **per-turn retrieval (RAG)** assembly; **token-accurate budgeting**
  that scales the working set to the model window; **chunked, lossless** long-artifact store with
  recency tie-breaking; a **`deep_query_memory`** tool for the relational (graph) path; and
  **parallel workers as memory citizens** (shared summary + per-item retrieval on read, single-writer
  reducer on write). All additive and fail-soft — with the warm/cold tiers down, a turn still
  completes. Server-side only (same HTTP contract, no new client coupling).
- **Recommended TypeScript terminal client** (`clients/ink/`) on a purpose-built renderer:
  slash-command autocomplete, local shell via `!cmd`, in-CLI `/update` (rebuild + reinstall),
  `/reset` + opt-in `/resume`, per-project session storage (`<codedir>/.ironclad-cli/`), and
  preserved + syntax-highlighted code display (`/cat` fences with the language from the extension).
- **Secure, session-gated channel** (single-tenant): selectable trust profiles
  `open` / `token` / `sealed`, a client-managed tunnel option, and an explicit session
  that seals on disconnect. The token is a deployment secret, not a user login.
- **Governed reasoning parallelism**: a fan-out governor (concurrency × max_tokens budget
  envelope) plus an in-engine `parallel_reason` tool. Conservative core defaults.
- **Function-calling robustness**: validate→reask on every tool argument, and recovery of
  tool calls from text for endpoints without native tool-calls (explicit markers only).
- **Runtime contract self-check**: `GET /doctor` + a boot summary.
- **Open plugin surface**: discover `skills/*.py` plugins from `GX10_PLUGINS_DIR` and
  expose each as an agent tool — no core change. See `docs/plugin-api.md`.
- **Pluggable code-agent CLI** via `GX10_AGENT_CMD` (not locked to Claude Code).
- **Dev environment**: `Dockerfile.dev` + `docker-compose.dev.yml` build + run the full
  test suite in a container (the build+test gate). See `docs/dev-environment.md`.
- Beginner on-ramp: `docs/self-maintenance.md` ("describe an idea, let the agents build
  it"), top-of-README quickstart.

### Fixed
- Headless code-agent could not write files without a permission mode (now
  `--permission-mode`, default `acceptEdits`).
- `/tasks` was readable without the deployment secret under the auth profiles (now gated).
- Tool-call text recovery could hijack a bare JSON answer into a destructive call
  (bare-object branch removed; explicit markers only).
- Auth-gate / router path normalization; request-body cap; tunnel child reaped on failure;
  config-tree skips hidden subdirs; UTF-8-safe output (no cp1252 crash).

### Notes
- Single-tenant by design; multi-user identity/authorization is not built (see
  `docs/roadmap.md`). Treat `main` as a development snapshot.
