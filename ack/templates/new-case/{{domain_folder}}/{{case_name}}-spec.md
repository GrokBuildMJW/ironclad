---
type: research
title: "{{case_title}} — Case-Spec"
date: {{date}}
author: ack.generator (scaffold)
domain: {{domain_name}}
tags: [spec, {{domain_name}}, {{case_name}}, planning]
---

# {{case_title}} — Case-Spec

> **Generator skeleton.** Fill in the sections, then refine the feature entry in the
> MAPPING of the [[{{domain_name}}-gap-tracking|gap-tracking file]]
> (phase/tier/depends_on/anchors). **Capability key:** `{{capability_key}}`.

## Context / problem

{{description}}

_(Why is this case needed? What concrete problem / iteration does it remove? Which
existing code is EXTENDED rather than rebuilt?)_

## Scope

- **In scope:** …
- **Out of scope:** auth/crypto (unless in scope), DB migrations, infra changes
  without explicit need.

## Acceptance criteria

- [ ] …
- [ ] Tests green (happy path + error path).
- [ ] No role boundaries crossed.
- [ ] Deliverable linked from the domain README.

## Schema / contract (if `task_json`-relevant)

If this case produces an LLM emission, derive the schema from the ACK SSOT
(`ack.case_spec.TaskSpec.model_json_schema()`) and emit via `emit_validated()` /
`emit_task_spec()` (`ack.validated_emit`). No hand-rolled required fields —
`capability` is enforced for buildable types when the Lodestar plugin is on.

## Verifications before building

- Verify codebase paths via search / from `anchors` — never guess.
- Check relevant prior context for `{{domain_name}} {{case_name}}`.

## See also
- [[{{domain_name}}-gap-tracking|Gap-tracking (full matrix)]]
- [[{{domain_name}}-backlog|Planning backlog]]
- [[README|{{domain_title}} — Domain index]]
