# Running Ironclad on other models (incl. regional open models)

Ironclad is **model-agnostic** — it talks to any **OpenAI-compatible** chat endpoint.
Switching models is a config change, not a code change: the reliability comes from the
Agent-Contract-Kernel (schema + validate→reask), not from a specific model.

## The only thing you change

```bash
export GX10_BASE_URL=http://<host>:<port>/v1   # your endpoint
export GX10_MODEL=<served-model-name>          # the name the endpoint serves
export GX10_API_KEY=...                         # only if the endpoint needs one
```

Then start the orchestrator (`python engine/server.py`) and drive it with a client — the
recommended TypeScript client (`ironclad`, after a one-time `npm install -g .` in
`clients/ink/`) or the zero-dependency Python REPL (`python engine/client.py --codedir .`).
That's it —
this connection path is the same one the reference deploy uses and is exercised throughout
the test suite.

> **Tool calling matters.** The orchestration engine uses function/tool calling. Pick
> an **instruct/chat** model and serve it behind an endpoint that supports tool calls
> (e.g. vLLM with `--enable-auto-tool-choice` and the model's matching
> `--tool-call-parser`). The ACK soft path (validate→reask) still adds reliability even
> where native tool-calling is weak. Always confirm the exact serving flags and
> tool-calling support against the **model's own card** — the notes below are starting
> pointers, not verified-on-our-hardware configs.

## Serving a model behind an OpenAI-compatible API (vLLM)

Generic shape — substitute the model id and the parser from its card:

```bash
vllm serve <hf-model-id-or-path> \
  --served-model-name my-model \
  --enable-auto-tool-choice --tool-call-parser <parser-from-model-card>
# → OpenAI API at http://localhost:8000/v1  → set GX10_BASE_URL/GX10_MODEL to match
```

## Regional open models

These are UAE/regional open models you can self-host and drive with Ironclad. Use the
**current model id and recommended serving flags from each project's official card** —
they evolve, so we link the direction rather than freeze details that could go stale.

### Falcon (TII)
Open instruct models from the Technology Innovation Institute (HF org **`tiiuae`**).
Serve an instruct variant with vLLM and point Ironclad at it. Check the model card for
the tool-calling parser it supports.

### Jais (Inception / G42 / MBZUAI)
Arabic-first, bilingual Arabic-English open models (HF org **`inceptionai`**). A strong
fit for Arabic agent workflows — note Ironclad's reply language is also configurable
(`GX10_LANGUAGE=ar`), so the chrome and the model can both be Arabic.

### K2 Think (MBZUAI / G42)
A reasoning-focused model. Use its hosted OpenAI-compatible API, or self-serve the
released weights behind vLLM; either way Ironclad connects via `GX10_BASE_URL` /
`GX10_MODEL`. For a reasoning model, you may want `GX10_THINKING=auto` (the default).

## Verify your connection

```bash
curl -s "$GX10_BASE_URL/models"            # your model should be listed
python engine/server.py --port 8100 &      # start the orchestrator, then drive it:
python engine/client.py --codedir .        # ...and ask a question
```

If `/models` lists your model and a turn returns an answer, Ironclad is wired to it.
Found a model that needs a tweak to work well? Open an issue — that feedback makes the
framework better for everyone.
