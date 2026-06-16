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

## 🚧 Status: in active development (pre-release)

Ironclad is **work in progress.** There is no tagged release yet; APIs, layout and
config may change without notice. It is generated from a private monorepo — treat
`main` as a development snapshot, not a stable artifact.

**Reference environment.** Ironclad is developed and exercised on an **NVIDIA DGX
Spark** (GB10, Blackwell `sm_121`, 128 GB unified memory) running a local **vLLM**
server with **Qwen3.6-35B-A3B-NVFP4**. Nothing is hard-wired to that box — any
OpenAI-compatible endpoint works — but the defaults (`localhost:8000`,
`qwen3.6-35b`), the throughput numbers and the constrained-decoding findings (NVFP4,
XGrammar on the CUDA 13 nightly) reflect that hardware. See
[`docs/dgx-spark.md`](docs/dgx-spark.md) for the full reference stack and a **one-shot
bootstrap** (`scripts/spark-bootstrap.sh`).

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

## Setup

Requires **Python 3.10+** and an **OpenAI-compatible endpoint** (e.g. vLLM).

```bash
git clone https://github.com/GrokBuildMJW/ironclad.git
cd ironclad
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\Activate.ps1
pip install -e ".[engine]"                         # ACK + engine (openai, prompt_toolkit)

# Point at your model endpoint (defaults: http://localhost:8000/v1, qwen3.6-35b):
export GX10_BASE_URL=http://localhost:8000/v1
export GX10_MODEL=your-served-model-name
export GX10_API_KEY=...                             # only if your endpoint needs one

# Monolithic full-screen CLI (one process):
python engine/gx10.py --workdir ./my-workspace
```

- **Full walkthrough, the server/client split, and the reference vLLM launch:**
  see **[`SETUP.md`](SETUP.md)** — including copy-paste **shell shortcuts** (so you can
  just type `ironclad`) for **Windows PowerShell, macOS and Linux**.
- **Let an AI coding agent set it up for you** (deterministic, verifiable runbook):
  see **[`AGENTS.md`](AGENTS.md)**.

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
