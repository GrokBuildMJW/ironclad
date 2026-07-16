---
capability: {{capability_key}}
kind: prompt
description: {{description|tojson}}
type: prompt
domain: {{domain_name}}
languages: [en, de]
variables: [input]
required: [input]
ask.input: What is the input this prompt should work on?
desc.input: The material the generated prompt operates on
version: "0.1.0"
provenance: generated
---
You are assisting with the following task: {{description}}

Work with this input:

{input}

Produce a clear, well-structured result. This is a generated prompt scaffold: edit
this template body and the frontmatter (variables, languages, elicitation) to fit
your task. It is already valid (ack.gate.gate_prompt) and usable as the
/{{capability_key}} slash-command — customise the wording before relying on it.
