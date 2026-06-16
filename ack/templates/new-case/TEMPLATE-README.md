# `new-case` — ACK Paved-Road Template

Scaffolds the **complete skeleton for a new Case or Domain** in one command:
Case-Spec + gap-tracking/backlog (with the exact markers/frontmatter
`ack.lodestar.tracking` consumes) + a procedural Skill stub + Tests + a
self-discovering registration stub + a domain README with backlinks.

> This kills the "many iterations until a backlog stands" hand-ritual: one command
> produces a correctly-wired, re-runnable skeleton.

## How to run

Canonical driver (stdlib, **no dependency**):

```bash
python -m ack.generator --domain todo-api --case my-feature \
    --description "What this case does"
# → <output-root>/Todo-Api/ (spec, gap-tracking, backlog, README, skills/, tests/)
```

Equivalent with Copier (only if `pip install copier` is present — optional):

```bash
copier copy <template-root> <output-root>
copier update <output-root>/Todo-Api   # re-run / propagate template upgrades
```

Both paths render the same `{{ token }}` tree. The CLI is the canonical
implementation; `copier.yml` keeps the questionnaire Copier-compatible.

## Re-runnable (3-way merge)

The CLI records the bytes it last rendered per file in
`<domain>/.ack-generator-state.json`. On re-run it does a diff3 merge of **base**
(last render) vs **mine** (on-disk, possibly hand-edited) vs **theirs** (fresh
render): template upgrades land, local edits survive, true divergence is written
with `<<<<<<< / ======= / >>>>>>>` markers and the run exits non-zero. Re-running
with identical answers is a no-op (idempotent).

## Tokens

`domain_name` · `domain_folder` · `domain_title` · `case_name` · `case_title` ·
`key_prefix` · `capability_key` (= `{key_prefix}-{case_name}`) · `description` ·
`phase` · `tier` · `type` · `assignee` · `effort` · `non_negotiable` ·
`tags_csv` · `tags_yaml` · `date`.

Rendering is **substitution-only** (no `str.format`/Jinja attribute access).

## Output layout (rendered into `--output-root`, default `cases/`)

```
{{domain_folder}}/
  {{domain_name}}-gap-tracking.md   # MAPPING (SSOT) + auto-regenerated TABLES markers
  {{domain_name}}-backlog.md        # placeholder; ack.lodestar.tracking overwrites it
  {{case_name}}-spec.md             # the Case-Spec
  README.md                         # domain index with backlinks
  skills/__init__.py                # self-discovering case registry (no hardcoding)
  skills/{{case_name}}.py           # Skill stub: CASE descriptor + run()
  tests/test_{{case_name}}.py       # pytest skeleton
```
