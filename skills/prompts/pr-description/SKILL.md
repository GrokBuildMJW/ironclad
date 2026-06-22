---
capability: pr-description
kind: prompt
description: Draft a clear, reviewer-focused pull-request description from a change summary
type: prompt
domain: engineering
languages: [en, de]
variables: [changes, issue, testing]
required: [changes]
ask.changes: Summarize what this PR changes (paste the diff summary or a short list).
ask.issue: Linked issue/ticket (e.g. #146)? Leave blank to omit.
ask.testing: How was it tested? Leave blank to omit.
desc.issue: Optional linked issue reference
desc.testing: Optional note on how the change was tested
version: "0.1.0"
provenance: built-in
---
Write a pull-request description for the following change. Be concrete and reviewer-focused; do not invent changes that are not listed.

Changes:
{changes}

Linked issue: {issue}
Testing: {testing}

Structure it as: a one-line **Summary** of intent, a **What changed** bullet list, a short **Why** paragraph, and a **Testing** note (use the testing info if given, otherwise state what a reviewer should check). Reference the linked issue if one is given. Keep it tight — no filler, no restating the diff line by line.
