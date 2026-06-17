# Ironclad

[![CI](https://github.com/GrokBuildMJW/ironclad/actions/workflows/ci.yml/badge.svg)](https://github.com/GrokBuildMJW/ironclad/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/ironclad-ai)](https://pypi.org/project/ironclad-ai/)
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
enforcement beats a large model you "trust" to format its output. You get
**production-grade tool-calling reliability without depending on any specific
model or parser** — the kernel enforces the contract, not the weights.

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
[You] > what is 17 times 23?
  [Qwen (planning)]
  17 times 23 is 391.
  [perf] TTFT 0.5s · 183 tok/2.9s = 64 tok/s · prompt 1739
  ======== ✓ DONE · ready · 1 gen · 3s · 183 tok ========
──────────────────────────────────────────────────────────────────────
│ [You] >
──────────────────────────────────────────────────────────────────────
 ██ Ironclad  powered by MJWC-AI-LAB
 ██  Orchestrator client · streaming   |   /help · exit · PageUp=history
     qwen3.6-35b · 64 tok/s · tasks 0P/0IP/0D · http://<server>:8100
```

**Reply language is a setting** (`GX10_LANGUAGE` — `en` default, `ar`, `fr`, …). The
model answers in the configured language regardless of the input language. Real output
with `GX10_LANGUAGE=ar`, same question:

```text
[You] > what is 17 times 23?
  حاصل ضرب 17 في 23 هو 391.
  ======== ✓ DONE · ready · 1 gen · 2s ========
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

## A reliability layer, not another model

Ironclad doesn't compete with the open models — it makes them **dependable to build
on**. It's **model-agnostic by design**: reliability comes from the contract kernel
(schema → validate → reask), not the weights. So it's a natural **agent/reliability
layer for regional open models** like Falcon, Jais and K2 Think — point it at any of
them ([running on other models](docs/models/)) and get fail-closed pipelines,
structured tool-calls and a thin local client, **without forking or retraining
anything**.

## A starting point to build on

Ironclad is a **foundation, not a finished product** — a generic agentic core meant
to be extended. Concrete use cases dock onto it as **vessels** (see
[`examples/demo-vessel/`](examples/demo-vessel/); a generator scaffolds new ones), so a
broad audience can build their own domain agents on a reliable, self-hosted base
rather than starting from scratch. Realistic directions the architecture supports today:

- **Edge & energy efficiency** — a small, enforced model on local/edge hardware
  instead of a large cloud one. That efficiency bet is the whole premise of the project.
- **Education · healthcare · logistics** — build a vessel for your domain: reliable
  tool-using agents and retrieval/RAG assistants over your own data, kept on-prem.

The repo's job is to give you a working starting point, not to ship every vertical —
the verticals are yours to build.

## Setup

Requires **Python 3.10+** and an **OpenAI-compatible endpoint** (e.g. vLLM).

**Install the library from PyPI:**

```bash
pip install ironclad-ai          # the ACK library (import ack)
pip install "ironclad-ai[engine]"  # + the orchestration engine deps
```

**Or clone for the full engine + CLI/TUI** (recommended while pre-release):

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

Honest split of **what works today** vs **what is planned** lives in
**[`docs/roadmap.md`](docs/roadmap.md)**; the per-component wiring status of shipped
features is in **[`docs/status.md`](docs/status.md)**. In short:

- **Today — single-tenant, home-LAN trust.** One operator, one principal, no
  authentication on the server (it trusts its network like the model port). Code stays
  on the client by construction.
- **In progress (Phase d) — secure, session-gated channel,** still single-operator:
  selectable trust profiles (`open` / `token` / `sealed`), a client-managed tunnel
  option, and an explicit session that **seals on disconnect**. The token is a
  *deployment secret*, not a user login.
- **Planned (Phase g) — Identity & Authorization (multi-tenant):** per-principal scope
  through tasks, memory namespaces, and data-source entitlements, with org/group
  RBAC via OIDC/SAML. **This does not exist yet** — until it lands, treat
  enterprise/government use as single-tenant on trusted infrastructure.
- Also: broader tests, verified recipes for more local open models, **RAG over local
  datasets** through the memory hook, and a first tagged release.

**Sovereign AI / local deployments.** Ironclad is **model-agnostic** and **fully
self-hostable** — it talks to any OpenAI-compatible endpoint, so it already runs
against locally-served open models (e.g. **Falcon**, **Jais**, **K2 Think** via vLLM —
see **[running on other models](docs/models/)**) with no cloud dependency and data kept
on your own infrastructure.

Issues and discussions are welcome — this is an early, openly-developed project.

## License

Apache License 2.0 — see [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).
Copyright 2026 MJWC-AI-LAB and Ironclad contributors.

---

🇦🇪 Built in the United Arab Emirates by **MJWC-AI-LAB**.
