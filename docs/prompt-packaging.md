# Prompt library & generator (design spec)

> **Design/planned** — the contract this targets. Built on the **core base**
> ([ADR-0002](adr/0002-core-always-on-skills.md)): always-on built-ins, `ack.i18n`,
> `ack.playbook`, `ack.catalogue`, `ack.gate`. See [ADR-0003](adr/0003-prompt-library.md) and
> [`status.md`](status.md). A prompt is a **core built-in — no plugin**.

A **prompt** is a declarative MD item (`kind: prompt`). Invoke it as a slash-command, answer a
few guided questions, and get a finished prompt in your target language. Add a new prompt by
**dropping a file** under `skills/` (no engine code change) — it is discovered always-on,
indexed by the catalogue, and validated by the gate.

## A prompt item (`kind: prompt`) — *available*

```
skills/<prompt>/
  SKILL.md          # frontmatter (prompt metadata + variables + languages) + the template body
  locales/          # optional <lang>.json overlays (translatable content), read via ack.i18n
```

`SKILL.md` frontmatter (extends the shared skill schema):

| Field | Required | Meaning |
|---|---|---|
| `capability` | yes | Unique id / catalogue key. |
| `kind` | yes | `prompt`. |
| `description` | yes | What the prompt produces / when to use it. |
| `type` / `domain` | recommended | Taxonomy (e.g. `type: prompt`, `domain: writing`). |
| `languages` | recommended | Supported codes (e.g. `[en, de]`); source defaults to `en`. |
| `variables` | yes | The inputs the generator elicits — each `{name, required, description}`. |
| `elicitation` | optional | Per-variable question text shown during the guided flow. |
| `version` / `provenance` | catalogue | semver + origin (built-in / user). |

The body is the **template** (placeholders for the variables). It reuses the `ack.playbook`
parser; it is a distinct **kind** from `playbook` (instructions the model reads) — a prompt is a
template the user fills to *produce* a prompt.

## Generation flow — *available*

Two surfaces drive the **same** `ack.promptgen.run_prompt` state machine (deterministic, LLM-free):

- **Direct, model-free** (`/<prompt-name>`) — list items with `/prompts`; invoke one with
  `/<name> [var=value …] [--lang xx]`. A single positional value (whole rest) fills the lone required
  variable (so `/explain-code <code>` works, `=`/`--lang` inside the value preserved); explicit
  `var=value` tokens set named variables. When all required values are present it **assembles** in
  the target language and returns the finished prompt; otherwise it returns the guiding questions for
  what is still missing.
- **Model-guided** (the `use_prompt` engine tool, `gx10._use_prompt`, surfaced in `_effective_tools`
  whenever any prompt is loaded) — call with no capability → **list**; call with a capability + a
  `values` JSON of what's collected so far → **guided elicitation** returns the **next** missing
  required variable's question (one at a time, using its `ask.<name>` text); once complete it
  **assembles** (render via `ack.i18n` in the **target** `lang`, source-language fallback) and
  returns a previewed prompt. The orchestrator drives the turn-by-turn Q&A.

Both render through `ack.i18n` (target language, source-language fallback). **Save** as a reusable
library item is the curated-library step.

## Multilingual — *available*

Built on the shared core **`ack.i18n`** (`Localizer(<item>/locales)`): source/target language
selection; a missing language falls back to the source. Add a language = drop a
`locales/<lang>.json` (no code change).

## Registration gate — *wired*

A prompt item passes `ack.gate.gate_prompt` before it is trusted: frontmatter schema valid
(`ack.prompt`, incl. `variables`/`languages`); every **required** variable actually appears as a
`{placeholder}` in the template (a required input that can't affect the output is a defect); and it
**assembles cleanly in every declared language** — each present `locales/<lang>.json` overlay must
be valid JSON with a non-empty `template` (a *missing* overlay is fine: it falls back to source).
A **strict** variant `gate_prompt(strict_locales=True)` makes a *missing* overlay for a declared
non-source language a failure ("declared == delivered") — this is the completeness check the
per-project library invariant (`library_items_complete`) applies to **generated** prompt items;
hand-authored built-ins stay on the lenient default.
`ack.gate.gate(<dir|SKILL.md>)` auto-routes `kind: prompt` items here. Deterministic, model-free;
the heavier behavioral `eval/` stays opt-in. **New prompt = drop an MD file** under `skills/`
— no engine change (see the shipped [starter library](../skills/prompts/README.md)).
