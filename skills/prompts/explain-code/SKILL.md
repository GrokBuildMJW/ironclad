---
capability: explain-code
kind: prompt
description: Explain a piece of code at a chosen level of detail
type: prompt
domain: engineering
languages: [en, de]
variables: [code, audience, depth]
required: [code]
ask.code: Paste the code to explain.
ask.audience: Who is the explanation for (e.g. a beginner, a reviewer, your future self)? Leave blank for a general developer.
ask.depth: How deep should it go (overview, line-by-line)? Leave blank for a balanced explanation.
desc.audience: Optional target reader
desc.depth: Optional level of detail
version: "0.1.0"
provenance: built-in
---
Explain the following code for {audience}, at this level of detail: {depth}.

```
{code}
```

Start with one sentence on what the code does overall. Then explain how it works, naming the key constructs and any non-obvious behaviour, edge cases, or assumptions. Be accurate before being brief: if something is unclear or looks wrong, say so rather than guessing.
