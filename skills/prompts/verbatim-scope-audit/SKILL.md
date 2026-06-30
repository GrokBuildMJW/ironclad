---
capability: verbatim-scope-audit
kind: prompt
description: Audit that a prompt's requirements are fully captured — enumerate V1..Vn and map each to a work item before any work starts (DEV-1 prose Verbatim→Scope audit)
type: prompt
domain: engineering
languages: [en, de]
variables: [requirements]
required: [requirements]
ask.requirements: Paste the operator's requirements / prompt (verbatim).
version: "0.1.0"
provenance: built-in
---
Audit the requirements below so that **nothing is silently dropped** before work starts.

Requirements (verbatim):
{requirements}

Do this:
1. **Enumerate** every atomic requirement as `V1..Vn` — one per line, in the operator's own wording; do not paraphrase in a way that loses scope.
2. **Map** each `Vi` to **at least one concrete work item** (a sub-issue / task). Mark any requirement that is already done or pure context as such, with a one-line justification.
3. **Flag the gaps** — any `Vi` with no work item, and any work item that maps to no requirement (scope creep).

Output a **Coverage matrix** table with the columns `| Vi | requirement | work item |`, then a one-line verdict: **complete** (every `Vi` is mapped) or the explicit list of unmapped requirements. Do **not** start work while any requirement is unmapped.
