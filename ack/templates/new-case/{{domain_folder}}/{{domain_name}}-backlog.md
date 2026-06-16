---
type: backlog
title: "{{domain_title}} — Planning Backlog"
date: {{date}}
updated: {{date}}
author: ack.generator (scaffold — overwritten by ack.lodestar.tracking)
domain: {{domain_name}}
tags: [backlog, {{domain_name}}, planning]
---

# {{domain_title}} — Planning Backlog

> **Generator placeholder.** The source of truth is the MAPPING in
> [[{{domain_name}}-gap-tracking|{{domain_name}}-gap-tracking.md]] + the TaskStore.
> Once you run `python -m ack.lodestar.tracking --root .`, this file is **fully
> regenerated** (rank-ordered, with handover seeds). Do not hand-edit.

## For the orchestrator (planning & creation)

1. **Take the TOP entry.**
2. Create the handover + task via `stage_handover`.
3. **Required:** set `capability: "<key>"` in the task JSON → drift-free status.

---

### 1. `{{capability_key}}` — {{case_title}}
- **Status:** 🔴 not-started · **Phase:** {{phase}} · **Tier:** {{tier}}
- **Proposal:** `type={{type}}` · `effort={{effort}}` · `assigned_to={{assignee}}`
- **Scope / gap:** {{description}}
- **Required task field:** `"capability": "{{capability_key}}"`

---

## See also
- [[{{domain_name}}-gap-tracking|Gap-tracking (full matrix)]]
- [[README|{{domain_title}} — Domain index]]
