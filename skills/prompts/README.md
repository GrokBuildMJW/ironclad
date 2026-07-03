# Starter prompt library

Curated, multilingual `kind: prompt` items that ship as **core built-ins** — they load at startup
(no config). List them with `/prompts`; invoke one directly with `/<name> [var=value …] [--lang xx]`
(deterministic, model-free); or drive the same items through the `use_prompt` tool for a model-guided
flow (list → guided elicitation → assemble in your language). See
[`prompt-packaging.md`](../../docs/prompt-packaging.md) and
[ADR-0003](../../docs/adr/0003-prompt-library.md).

| Prompt | What it produces | Required inputs | Languages |
|--------|------------------|-----------------|-----------|
| `code-review` | A focused, actionable code-review prompt for a diff | `diff` | en, de |
| `commit-message` | A Conventional-Commits message from a change description | `changes` | en, de |
| `bug-report` | A structured, reproducible bug report | `summary`, `steps`, `expected`, `actual` | en, de |
| `explain-code` | An explanation of a piece of code at a chosen depth | `code` | en, de |
| `pr-description` | A clear, reviewer-focused pull-request description | `changes` | en, de |
| `refactor-plan` | A safe, incremental step-by-step refactoring plan | `code` | en, de |
| `test-plan` | A focused, prioritised test plan for a change | `change` | en, de |
| `feature-spec` | A concise product feature spec / PRD (problem, users, goals, requirements, acceptance) | `feature` | en, de |
| `dev-process` | A disciplined C0→C1→C2 dev-loop plan (readiness / per-unit / completion), docs-as-code, no-guessing | `task`, `change_type` | en, de |
| `verbatim-scope-audit` | A Verbatim→Scope audit: enumerate a prompt's requirements V1..Vn and map each to a work item before work starts | `requirements` | en, de |
| `dev-loop-runner` | Run one unit through the light dev loop (select → work → review → done) over the CLI-agnostic handover | `unit` | en, de |

## Add your own — drop an MD file

A new prompt is **one file**, no engine change. Create `<name>/SKILL.md` with a `kind: prompt`
frontmatter (capability, description, `variables`, `required`, optional `ask.<var>` questions,
`languages`) and a template body using `{variable}` placeholders. Add a translation by dropping
`<name>/locales/<lang>.json` with a `"template"` key. Every item must pass `ack.gate` — its
required variables must appear in the template and each declared language must assemble cleanly.
