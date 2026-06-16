---
domain: todo-api
title: "Todo-API — Gap-Tracking"
updated: 2026-06-16
---

# Todo-API — Gap-Tracking

A tiny demo capability domain for Ironclad / Lodestar. The MAPPING below is the
single source of truth; the tables and the sibling backlog are **generated** from
it + the TaskStore by `ack.lodestar.tracking` — do not edit them by hand.

<!-- MAPPING-START -->
```json
{
  "features": [
    {"key": "todo-list", "feature": "List todos", "phase": "MVP", "tier": "high",
     "non_negotiable": true, "notes": "GET /todos with paging."},
    {"key": "todo-create", "feature": "Create a todo", "phase": "MVP", "tier": "high",
     "notes": "POST /todos with validation."},
    {"key": "todo-auth", "feature": "Authenticated access", "phase": "V1", "tier": "high",
     "type": "security", "depends_on": ["todo-create"],
     "notes": "Bearer-token auth on all routes."}
  ]
}
```
<!-- MAPPING-END -->

## Status

<!-- TABLES-START -->
*Auto-generated 2026-06-16 via ack.lodestar.tracking — do not edit by hand.*

### Metrics

| Status | Count |
|--------|-------|
| ✅ implemented | 1 |
| 🟡 partial | 0 |
| ⏳ in-progress | 0 |
| 🔴 not-started | 2 |
| ⚪ out-of-scope | 0 |
| **Total** | **3** |

### Full feature matrix

| Feature | Phase | Status | Tasks | NN | Sources |
|---------|-------|--------|-------|----|---------|
| Create a todo | MVP | 🔴 not-started | — |  |  |
| List todos | MVP | ✅ implemented | DEMO-1 | 🔒 |  |
| Authenticated access | V1 | 🔴 not-started | — |  |  |

### Open gaps & partial implementations

| Feature | Phase | Status | NN | Gap / next step |
|---------|-------|--------|----|-----------------|
| Create a todo | MVP | 🔴 not-started |  | POST /todos with validation. |
| Authenticated access | V1 | 🔴 not-started |  | Bearer-token auth on all routes. |

> NN legend: 🔒 = non-negotiable.
<!-- TABLES-END -->
