---
capability: commit-message
kind: prompt
description: Draft a Conventional-Commits message from a description of the changes
type: prompt
domain: engineering
languages: [en, de]
variables: [changes, type, scope]
required: [changes]
ask.changes: Describe what changed (paste the diff summary or list the changes).
ask.type: Commit type (feat, fix, docs, refactor, test, chore)? Leave blank to let it be inferred.
ask.scope: Optional scope (the affected area/module). Leave blank to omit.
desc.type: Conventional-Commits type
desc.scope: Optional affected area
version: "0.1.0"
provenance: built-in
---
Write a single Conventional-Commits message for the following changes. Prefer type "{type}" and scope "{scope}" if given; otherwise infer them.

Changes:
{changes}

Output exactly one commit: a `type(scope): subject` header in the imperative mood under 72 characters, a blank line, then a body that explains the what and the why (not the how) in short wrapped lines. Do not invent changes that are not described above.
