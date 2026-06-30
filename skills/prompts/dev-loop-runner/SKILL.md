---
capability: dev-loop-runner
kind: prompt
description: Run one unit of work through the light DEV-1 loop — select → work → review → done — over the CLI-agnostic handover, discipline only (no native guards)
type: prompt
domain: engineering
languages: [en, de]
variables: [unit]
required: [unit]
ask.unit: What is the unit of work (the spec / item to run)?
version: "0.1.0"
provenance: built-in
---
Run the unit **"{unit}"** through one pass of the **light (DEV-1)** loop. This tier is **discipline, not enforcement**: the loop is a procedure you follow yourself — there are no native guards stopping you — so hold the line by hand.

**Select** — confirm "{unit}" is the right next thing: its dependencies are done, it is not blocked, and it is one focused unit (not a bundle). If it is not, pick the correct unit first.

**Work** — run it over the per-unit, CLI-agnostic handover contract:
- Read the handover brief for "{unit}" (the task, the working copy, the acceptance signal).
- Do the work with your configured code-agent against the local working copy. Keep it small, focused, and evidence-based; write the tests with the code.
- Keep the docs current in the **same** change (docs-as-code) — leave no drift.
- Write the feedback back to the handover (what changed, tests run, result).

**Review** — a light, scoped self-review of the diff: tests written and green, docs updated, and nothing outside the scope of "{unit}". Code gets a real read; a docs-only change rides the doc checks.

**Done** — record the outcome (what changed, tests green, docs current), then advance to the next unit. DEV-1 is **stateless and single-unit**: finish this one cleanly before selecting the next; do not carry a backlog in your head.

**No guessing:** at a genuine architecture fork or an ambiguity this loop does not resolve, **stop and ask** — do not guess your way past it.
