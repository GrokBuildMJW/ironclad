# ADR-0003 — Prompt library & multilingual prompt generator

- **Status:** Accepted (design) — implementation under epic #105 (sub-issues #108, #109, #110, #111). Records the design; not a claim it ships yet (see [`status.md`](../status.md)).
- **Date:** 2026-06-21
- **Context sources:** the research catalogue `plan_skill_libary.md` (prompt/skill patterns), `skills/mpr/` (the real skill format + `i18n` usage), and the **core base** from [ADR-0002](0002-core-always-on-skills.md) (always-on built-in loader, shared `ack.i18n`, `ack.playbook`, `ack.catalogue`, `ack.gate`).

## Context

Make prompting easy: a curated, multilingual **prompt library** + a generator. A user invokes a prompt as a slash-command ("prompt as a function"), is **guided (elicitation)** through intent/variables, and gets a finished, high-quality prompt in the target language. After #112 the substrate is **core, always-on** — built-ins load from `skills/` (no `GX10_PLUGINS_DIR`), content i18n is `ack.i18n`, and `ack.catalogue`/`ack.gate`/`ack.playbook` exist. So the prompt library is built **on the core engine, as a core built-in — no plugin, no parallel infrastructure.**

There is **no existing prompt library to migrate** (verified by grep — only the orchestrator system prompt + MPR's synthesis-internal prompt construction exist); the i18n migration was already done in #112 (#107). So this epic is additive.

## Decisions

**D1 — A prompt is a declarative MD item, `kind: prompt`, a core built-in.** It lives under `skills/` (always-on via the #114 loader), reuses the `ack.playbook` parser (frontmatter + body + lazy references), is indexed by `ack.catalogue` (semver + provenance), and is validated by `ack.gate`. A **Python prompt** (`skills/mpr`-style) remains available for coding cases (dual format). **No new registry/loader; no plugin.**

**D2 — `kind: prompt` is distinct from `kind: playbook`.** A playbook is *instructions the model reads* (loaded via `use_skill`). A prompt-library item is a *template the user fills via elicitation to produce a finished prompt* — a different runtime (deterministic assembly, not context-loading). It shares the parser/catalogue/gate but has its own assembler + slash/elicitation runner.

**D3 — Frontmatter schema** (extends the shared metadata): `capability`, `kind: prompt`, `description`, `type`/`domain`, **`languages`** (e.g. `[en, de]`; source default `en`), **`variables`** (each `{name, required, description}`), optional **`elicitation`** (per-variable question text); `version`/`provenance`. The body is the **template** (placeholders for the variables).

**D4 — Multilingual assembly via `ack.i18n`.** Render the finished prompt from the template + variable values in a chosen **target language**, using a per-item `locales/` overlay through `ack.i18n.Localizer`; source/target selection; missing language → source-language fallback. LLM-free deterministic rendering.

**D5 — Slash-command + guided elicitation (one command surface).** The engine command router resolves `/<prompt-name>` against the prompt catalogue and runs **guided elicitation** (ask each missing required variable) → **assemble** → **preview** → offer to **save** as a reusable library item. Extends the existing router (no second surface); the client offers prompt names via catalogue-backed autocomplete.

**D6 — Registration gate (reused).** A prompt item passes `ack.gate` before it is trusted: frontmatter schema valid (incl. `variables`/`languages`), references/locales readable. **"New prompt = drop an MD file"** under `skills/` (no engine code change) + it passes the gate. Behavioral `eval/` stays opt-in.

## Boundary / security

Secret-free; **English-only code/docs**. Prompt templates + `locales/*.json` are **translatable data** (the multilingual feature), not a code-language violation. Built-ins ship in the core export; ACK contracts apply.

## Consequences

- Prompts are first-class core built-ins: available out of the box, discoverable in the catalogue, gated, multilingual.
- The command router grows prompt-resolution + an elicitation loop (the one new engine surface).
- The user-defined / 3rd-party prompt libraries ride the existing plugin surface (`GX10_PLUGINS_DIR`) later (soft dep on #20 for per-principal libraries).

## Alternatives considered

- **Reuse `kind: playbook` for prompts** — rejected: the elicitation→assemble flow is a different runtime than context-loading instructions; a distinct `kind: prompt` keeps both clean (shared parser, separate runner).
- **A separate prompt registry/loader/i18n** — rejected: parallel infrastructure, against the core-reuse mandate (#112).
- **Plugin-loaded prompts** — rejected: prompts are first-class built-ins now (#112), always-on; 3rd-party prompts still ride the plugin surface.
