---
capability: test-plan
kind: prompt
description: Draft a focused, prioritised test plan for a change or feature
type: prompt
domain: engineering
languages: [en, de]
variables: [change, framework]
required: [change]
ask.change: Describe the change/feature to test (paste the diff summary or a short description).
ask.framework: Test framework/stack (e.g. pytest, node:test)? Leave blank if unknown.
desc.framework: Optional test framework hint
version: "0.1.0"
provenance: built-in
---
Draft a focused test plan for the following change. Target framework: {framework}.

{change}

List the test cases as a prioritised checklist: happy path, edge cases, error/failure modes, and regression risks. For each case give the input or condition and the expected result. Flag what should be a new test versus an existing one to extend, and note anything that cannot be tested deterministically (and how to handle it). Keep it concrete — no vague "test everything".
