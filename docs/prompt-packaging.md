# Prompt library & generator (design spec)

> **Design/planned** — the contract this targets (epic #105). Built on the **core base**
> ([ADR-0002](adr/0002-core-always-on-skills.md)): always-on built-ins, `ack.i18n`,
> `ack.playbook`, `ack.catalogue`, `ack.gate`. See [ADR-0003](adr/0003-prompt-library.md) and
> [`status.md`](status.md). A prompt is a **core built-in — no plugin**.

A **prompt** is a declarative MD item (`kind: prompt`). Invoke it as a slash-command, answer a
few guided questions, and get a finished prompt in your target language. Add a new prompt by
**dropping a file** under `skills/` (no engine code change) — it is discovered always-on,
indexed by the catalogue, and validated by the gate.

## A prompt item (`kind: prompt`) — *available (#108)*

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

## Generation flow — *available (#109/#110)*

A discovered prompt is exposed as the engine tool **`use_prompt`** (`gx10._use_prompt`, surfaced in
`_effective_tools` whenever any prompt is loaded). The flow: call with no capability → **list** the
prompts; call with a capability + a `values` JSON of what's collected so far → **guided
elicitation** returns the **next** missing required variable's question (one at a time, using its
`ask.<name>` text); once all required values are present it **assembles** (render the template via
`ack.i18n` in the **target** `lang`, source-language fallback) and returns a previewed prompt. The
state machine is `ack.promptgen.run_prompt` (deterministic, LLM-free); the orchestrator drives the
turn-by-turn Q&A. **Save** as a reusable library item is the curated-library step (#111).

## Multilingual — *available (#109)*

Built on the shared core **`ack.i18n`** (`Localizer(<item>/locales)`): source/target language
selection; a missing language falls back to the source. Add a language = drop a
`locales/<lang>.json` (no code change).

## Registration gate — *wired (#111)*

A prompt item passes `ack.gate.gate_prompt` before it is trusted: frontmatter schema valid
(`ack.prompt`, incl. `variables`/`languages`); every **required** variable actually appears as a
`{placeholder}` in the template (a required input that can't affect the output is a defect); and it
**assembles cleanly in every declared language** — each present `locales/<lang>.json` overlay must
be valid JSON with a non-empty `template` (a *missing* overlay is fine: it falls back to source).
`ack.gate.gate(<dir|SKILL.md>)` auto-routes `kind: prompt` items here. Deterministic, model-free;
the heavier behavioral `eval/` stays opt-in. **New prompt = drop an MD file** under `skills/`
— no engine change (see the shipped [starter library](../skills/prompts/README.md)).
