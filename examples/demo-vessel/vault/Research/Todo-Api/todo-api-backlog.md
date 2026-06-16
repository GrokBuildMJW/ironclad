---
type: backlog
title: "Todo-API — Planning Backlog"
date: 2026-06-16
updated: 2026-06-16
author: ack.lodestar.tracking (auto)
domain: todo-api
tags: [backlog, todo-api, planning]
---

# Todo-API — Planning Backlog

> **Auto-generated — do not edit by hand.** Source: the MAPPING in
> [[todo-api-gap-tracking|todo-api-gap-tracking.md]] + the TaskStore.
> Order: phase MVP→V1→V2→V3 → non-negotiable (🔒) → tier.
>
> **🟡 partial = OPEN, not done** — the remaining scope IS the task. Do not skip.
> Always take the **top** entry (whether partial or not-started).

## For the orchestrator (planning & creation)

1. **Take the TOP entry** (rank #1). Deviate only with an operator reason.
2. Create the handover + task via `stage_handover` — use the seed below.
3. **Required:** set `capability: "<key>"` in the task JSON → drift-free status.
4. Codebase paths ONLY from `anchors` or verified via search — never guessed.

**Open total:** 1 · of which non-negotiable: 0

---

### 1. `todo-create` — Create a todo
- **Status:** 🔴 not-started · **Phase:** MVP · **Tier:** high
- **Proposal:** `type=implementation` · `effort=high` · `assigned_to=claude-opus-4-8`
- **Scope / gap:** POST /todos with validation.

- **Required task field:** `"capability": "todo-create"`

---

## ⏸ Blocked (depends_on unmet — not yet plannable)

- `todo-auth` — Authenticated access · **waiting on:** todo-create

---

## See also
- [[todo-api-gap-tracking|Gap-tracking (full matrix)]]
