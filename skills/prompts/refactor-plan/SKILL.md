---
capability: refactor-plan
kind: prompt
description: Produce a safe, incremental, step-by-step refactoring plan for a piece of code
type: prompt
domain: engineering
languages: [en, de]
variables: [code, goal]
required: [code]
ask.code: Paste the code (or describe the module) to refactor.
ask.goal: What is the refactoring goal (e.g. reduce duplication, extract a seam)? Leave blank for a general cleanup.
desc.goal: Optional refactoring objective
version: "0.1.0"
provenance: built-in
---
You are a senior engineer planning a refactor. Propose a safe, incremental plan for the following code, working toward this goal: {goal}.

{code}

Give an ordered list of small, independently-verifiable steps. For each step state: the change, why it is behavior-preserving, and how to verify it (a test or a concrete check). Call out risks and any step that needs a test added first. Do not rewrite the code — plan the path. Preserve behavior unless a change is explicitly justified.
