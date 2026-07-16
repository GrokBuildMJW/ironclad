---
type: tracking
title: "{{domain_title}} — Gap-Tracking"
date: {{date}}
updated: {{date}}
author: ack.generator (scaffold) + ack.lodestar.tracking (tables auto)
domain: {{domain_name}}
tags: {{tags_yaml}}
---

# {{domain_title}} — Gap-Tracking

> **Living table (Lodestar).** The **MAPPING** below (JSON) is the SSOT.
> Status / tables / backlog are computed from the **TaskStore** by
> `ack.lodestar.tracking` — restart-safe, NO manual marking. A build task carries
> `capability: "<key>"`; once it lands in `tasks/done/`, the increment counts as done.
>
> **Purpose:** {{description}}
> Order via `depends_on` (foundation first). `assignee`/effort are proposals — the
> orchestrator routes finally.
>
> Regenerate: `python -m ack.lodestar.tracking --root .` · Queue: [[{{domain_name}}-backlog|{{domain_name}}-backlog.md]]

## Status legend

| Status | Meaning |
|--------|---------|
| ✅ implemented | Increment built (task done) |
| 🔴 not-started | Not built yet |

---

<!-- MAPPING-START -->
```json
{
  "features": [
    {"key": "{{capability_key}}", "feature": "{{case_title}}", "phase": "{{phase}}", "tier": "{{tier}}", "type": "{{type}}", "assignee": "{{assignee}}", "effort": "{{effort}}", "non_negotiable": {{non_negotiable}}, "task_ids": [], "sources": [], "anchors": [], "depends_on": [], "notes": {{description|tojson}}}
  ]
}
```
<!-- MAPPING-END -->

<!-- TABLES-START -->
*Not generated yet — run `python -m ack.lodestar.tracking --root .`.*
<!-- TABLES-END -->

## See also
- [[{{domain_name}}-backlog|Planning backlog (auto-generated)]]
- [[README|{{domain_title}} — Domain index]]
