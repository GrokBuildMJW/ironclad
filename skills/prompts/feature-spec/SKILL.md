---
capability: feature-spec
kind: prompt
description: Draft a concise product feature spec / PRD (problem, users, goals, requirements, acceptance) from an idea
type: prompt
domain: product
languages: [en, de]
variables: [feature, context]
required: [feature]
ask.feature: Describe the feature or idea to specify (what should it do, and for whom?).
ask.context: Any context/constraints (target users, platform, deadline, related systems)? Leave blank if none.
desc.context: Optional context/constraints hint
version: "0.1.0"
provenance: built-in
---
Draft a concise product feature specification (PRD) for the following feature. Context/constraints: {context}.

{feature}

Produce these sections:
- **Problem** — the user pain and why it matters now (not the solution).
- **Users & use cases** — who it is for and the top jobs-to-be-done.
- **Goals** and **Non-goals** — explicit scope boundaries (what this will and will NOT do).
- **Requirements** — a prioritised list marked MUST / SHOULD / COULD.
- **Acceptance criteria** — observable, testable, one per MUST requirement.
- **Risks & open questions** — what could go wrong and what needs a decision before build.

Keep it concrete and buildable — no marketing language, no vague "improve the UX".
