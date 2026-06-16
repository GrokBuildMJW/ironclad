# Ironclad

[![CI](https://github.com/GrokBuildMJW/ironclad/actions/workflows/ci.yml/badge.svg)](https://github.com/GrokBuildMJW/ironclad/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Status](https://img.shields.io/badge/status-pre--release-orange)
[![Stars](https://img.shields.io/github/stars/GrokBuildMJW/ironclad?style=social)](https://github.com/GrokBuildMJW/ironclad/stargazers)

**Reliability for LLM agents through enforcement, not model size.**
🇦🇪 Built in the UAE by MJWC-AI-LAB.

Ironclad is a generic, model-agnostic framework for building reliable agentic
systems. It pairs an **Agent-Contract-Kernel (ACK)** — schema-as-single-source-
of-truth, validate→reask→retry, a generator and a preflight doctor — with a
lean **orchestration engine** that turns multi-step agent workflows into
deterministic, fail-closed pipelines.

The guiding principle: a small fast model with hard schema/validation
enforcement beats a large model you "trust" to format its output. Hermes-grade
tool-calling reliability **without** depending on any specific model or parser.

## 🚧 Status: proven core, mid-redesign (pre-release)

Ironclad's engine comes from a **proven, in-production orchestrator**, but it is
currently undergoing a **complete redesign** (single process → headless server + thin
client, containerized, reasoning-worker fan-out, full-screen TUI). **Not everything is
re-wired or re-tested yet**, and some features are still **placeholders** while the
rebuild finishes (notably memory and autoplan). There is no tagged release; APIs,
layout and config may change. Treat `main` as a development snapshot.

👉 **[`docs/status.md`](docs/status.md)** is the honest, per-component **wiring status**
(proven / wired+tested / placeholder / opt-in), the module reference, the memory
situation, and the latest load-test results. Read it before relying on anything.

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

## Demo

The full-screen client over the server/client split — a turn streams live, the
toolbar shows live status (model · throughput · tasks · watcher). Illustrative
transcript from a real session:

```text
[Du] > was ist 17 mal 23?
  [Qwen plant (Thinking)]
  17 mal 23 ergibt 391.
  [perf] TTFT 0.5s · 183 tok/2.9s = 64 tok/s · prompt 1739
  ======== ✓ FERTIG · bereit für Eingabe · 1 Gen · 3s · 183 tok ========
──────────────────────────────────────────────────────────────────────
│ [Du] >
──────────────────────────────────────────────────────────────────────
 ██ Ironclad  powered by MJWC-AI-LAB
 ██  Orchestrator-Client · streaming   |   /help · exit
     qwen3.6-35b · 64 tok/s · tasks 0P/0IP/0D · http://<server>:8100
```

`/command` routing (local + forwarded), scrollback (PageUp/PageDown) and compressed
multi-line paste are built in. There's also a plain line REPL and a monolithic CLI.

## Benchmarks

Measured on the reference stack — a single **NVIDIA DGX Spark** (GB10) serving
**Qwen3.6-35B-A3B-NVFP4** via vLLM, driven over the LAN:

| Workload | Result |
|----------|--------|
| Reasoning **fan-out**, 8 independent prompts | **5.8× faster** than serial (1.2 s vs 7.1 s), ~118 tok/s aggregate |
| Conversational turn (single agent) | ~55–68 tok/s, ~2.1 s mean latency |
| Structured emission (ACK, thinking-off) | 100% schema-valid in earlier measurement |

Reproduce with your own endpoint; numbers scale with the model and GPU. Full method
and the per-component **wiring status** live in [`docs/status.md`](docs/status.md).

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

## Roadmap

Honest near-term plan (the rebuild's placeholders are now wired — see
[`docs/status.md`](docs/status.md) for the full per-component status):

- Broaden test coverage and harden the new server/client paths.
- Single-command compose for the whole reference stack (model + memory + orchestrator).
- First tagged release once the APIs settle.

Issues and discussions are welcome — this is an early, openly-developed project.

## License

Apache License 2.0 — see [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).
Copyright 2026 MJWC-AI-LAB and Ironclad contributors.

---

🇦🇪 Built in the United Arab Emirates by **MJWC-AI-LAB**.
