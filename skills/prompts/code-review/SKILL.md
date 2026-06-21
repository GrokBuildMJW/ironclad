---
capability: code-review
kind: prompt
description: Produce a focused, actionable code-review prompt for a diff
type: prompt
domain: engineering
languages: [en, de]
variables: [diff, focus, language]
required: [diff]
ask.diff: Paste the diff or code to review.
ask.focus: What should the review focus on (e.g. security, performance, readability)? Leave blank for a general review.
ask.language: What programming language is this? Leave blank if it is obvious from the code.
desc.focus: Optional review emphasis
desc.language: Optional programming language hint
version: "0.1.0"
provenance: built-in
---
You are a meticulous senior engineer reviewing a code change. Review the following {language} change, focusing on {focus}.

```
{diff}
```

Report your findings as a prioritised list (most important first). For each finding give: the location, why it matters, and a concrete suggested fix. Call out correctness bugs and security issues before style. If the change is sound, say so plainly and note anything worth watching.
