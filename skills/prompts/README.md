# Starter prompt library

Curated, multilingual `kind: prompt` items that ship as **core built-ins** — they load at startup
(no config) and are offered through the `use_prompt` tool: list → guided elicitation → assemble in
your language. See [`prompt-packaging.md`](../../docs/prompt-packaging.md) and
[ADR-0003](../../docs/adr/0003-prompt-library.md).

| Prompt | What it produces | Required inputs | Languages |
|--------|------------------|-----------------|-----------|
| `code-review` | A focused, actionable code-review prompt for a diff | `diff` | en, de |
| `commit-message` | A Conventional-Commits message from a change description | `changes` | en, de |
| `bug-report` | A structured, reproducible bug report | `summary`, `steps`, `expected`, `actual` | en, de |
| `explain-code` | An explanation of a piece of code at a chosen depth | `code` | en, de |

## Add your own — drop an MD file

A new prompt is **one file**, no engine change. Create `<name>/SKILL.md` with a `kind: prompt`
frontmatter (capability, description, `variables`, `required`, optional `ask.<var>` questions,
`languages`) and a template body using `{variable}` placeholders. Add a translation by dropping
`<name>/locales/<lang>.json` with a `"template"` key. Every item must pass `ack.gate` — its
required variables must appear in the template and each declared language must assemble cleanly.
