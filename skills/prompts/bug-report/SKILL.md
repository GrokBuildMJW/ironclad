---
capability: bug-report
kind: prompt
description: Turn a rough observation into a structured, reproducible bug report
type: prompt
domain: engineering
languages: [en, de]
variables: [summary, steps, expected, actual]
required: [summary, steps, expected, actual]
ask.summary: One-line summary of the bug.
ask.steps: What are the exact steps to reproduce it?
ask.expected: What did you expect to happen?
ask.actual: What actually happened?
version: "0.1.0"
provenance: built-in
---
Write a clear, reproducible bug report from the details below. Keep it factual and concise; do not speculate about the cause beyond what the evidence supports.

Summary: {summary}

Steps to reproduce:
{steps}

Expected behaviour: {expected}

Actual behaviour: {actual}

Format the report with the headings: **Summary**, **Steps to reproduce** (as a numbered list), **Expected**, **Actual**, and a final **Severity** you justify in one sentence.
