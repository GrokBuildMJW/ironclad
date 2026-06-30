# `new-prompt` — ACK Prompt-Library Scaffold

Scaffolds a **`kind: prompt` library item** in one command: a `SKILL.md`
(frontmatter + template body) plus a `locales/<lang>.json` overlay. The rendered
item is **valid on first render** — it passes `ack.gate.gate_prompt` (frontmatter
schema, required variables used in the template, assembles in every declared
language) — so you generate, then customise the wording.

> A prompt has no executable `run()` to fill in (unlike the `new-case` tool
> scaffold). It is "complete" the moment it is gate-valid; the paved road therefore
> renders a working, fill-me-in template rather than a sentinel-marked stub.

## How to run

Canonical driver (stdlib, **no dependency**):

```bash
python -m ack.generator --kind prompt --domain writing --case blog-brief \
    --description "Draft a focused blog-post brief"
# → <output-root>/Writing/blog-brief/SKILL.md (+ locales/de.json)
```

Inside the engine, `/generate --kind prompt --domain … --case … --description …`
renders into the **active project's library** (`vault/library`), guarded so a
generated item can never shadow a core built-in.

## Output layout (rendered into `--output-root`)

```
{{domain_folder}}/
  {{case_name}}/
    SKILL.md              # kind: prompt — frontmatter + template body
    locales/
      de.json             # German overlay scaffold (ASCII-only, like the seeds)
```

## Customise

1. Edit the `description`, `variables`/`required`, and per-variable `ask.*`/`desc.*`
   elicitation in the frontmatter.
2. Rewrite the template body — every **required** variable must appear as a
   `{placeholder}` (the gate enforces this).
3. Translate `locales/de.json` (and add more `locales/<lang>.json` for each entry in
   `languages:`). A missing overlay falls back to the English source; a *present*
   one must be valid JSON with a non-empty `template`.

## Tokens

`domain_name` · `domain_folder` · `case_name` · `capability_key`
(= `{key_prefix}-{case_name}`) · `description`. Rendering is **substitution-only**
(`{{ token }}`); single-brace `{input}` is a *prompt* placeholder and is left
untouched by the generator. Re-runnable via the same 3-way merge as `new-case`.
