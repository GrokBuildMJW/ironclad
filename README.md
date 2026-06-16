# Ironclad

**Reliability for LLM agents through enforcement, not model size.**

Ironclad is a generic, model-agnostic framework for building reliable agentic
systems. It pairs an **Agent-Contract-Kernel (ACK)** — schema-as-single-source-
of-truth, validate→reask→retry, a generator and a preflight doctor — with a
lean **orchestration engine** that turns multi-step agent workflows into
deterministic, fail-closed pipelines.

The guiding principle: a small fast model with hard schema/validation
enforcement beats a large model you "trust" to format its output. Hermes-grade
tool-calling reliability **without** depending on any specific model or parser.

> Status: **early / pre-release.** This README is the public entry point; the
> framework is being consolidated from a working internal codebase. APIs will
> stabilize before the first tagged release.

## Why

- **Contract-first.** One Pydantic schema drives the prompt, the validator, the
  docs, and (where the hardware allows) constrained decoding. No drift between
  what you ask for and what you check.
- **Fail-closed pipelines.** Macro steps (e.g. task hand-off, advancement) do
  the mechanical file work deterministically in code, not in model turns —
  fewer round-trips, no silent half-completions.
- **Model-agnostic.** Swap the orchestrator model freely; reliability comes
  from the kernel, not the weights.
- **Standalone.** No hidden dependency on any private deployment. Bring your
  own OpenAI-compatible endpoint (vLLM, etc.).

## Quickstart

```bash
pip install -r requirements.txt        # (coming with the first release)

# Point at any OpenAI-compatible endpoint:
export IRONCLAD_BASE_URL=http://localhost:8000/v1
export IRONCLAD_MODEL=your-served-model-name
export IRONCLAD_API_KEY=...            # if your endpoint needs one

python -m ironclad --workdir ./my-workspace
```

A runnable **demo vessel** lives in [`examples/demo-vessel/`](examples/demo-vessel/)
— a minimal, self-contained workspace showing a contract spec, a pipeline, and
the doctor preflight. Real vessels stay in the operators' own private repos.

## Layout

```
core/
  ack/                 # Agent-Contract-Kernel: case-spec, validated-emit, registry, doctor, generator
  engine/              # orchestration engine: agent loop, task store, fail-closed macros
  examples/demo-vessel # runnable example workspace
  LICENSE  NOTICE      # Apache-2.0
```

## License

Apache License 2.0 — see [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).
Copyright 2026 MJWC-AI-LAB and Ironclad contributors.
