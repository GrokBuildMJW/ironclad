# Ironclad

[![CI](https://github.com/GrokBuildMJW/ironclad/actions/workflows/ci.yml/badge.svg)](https://github.com/GrokBuildMJW/ironclad/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/ironclad-ai)](https://pypi.org/project/ironclad-ai/)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![TypeScript](https://img.shields.io/badge/TypeScript-5-3178c6?logo=typescript&logoColor=white)
![Node](https://img.shields.io/badge/Node-%E2%89%A522-3c873a?logo=node.js&logoColor=white)
![Status](https://img.shields.io/badge/status-pre--release-orange)
[![Stars](https://img.shields.io/github/stars/GrokBuildMJW/ironclad?style=social)](https://github.com/GrokBuildMJW/ironclad/stargazers)

**Reliability for LLM agents through enforcement, not model size.**
🇦🇪 Built in the UAE by MJWC-AI-LAB.

## Try it — describe an idea, let the agents build it

```bash
git clone https://github.com/GrokBuildMJW/ironclad.git && cd ironclad
pip install -e ".[engine]"
export GX10_BASE_URL=http://localhost:8000/v1 GX10_MODEL=your-model   # your model endpoint

python engine/server.py &              # 1) the orchestrator (the agent + state)
python engine/client.py --codedir .    # 2) the client — drives it; type what you want
#   Read README.md and summarise it.   /   Build add(a,b) in calc.py with a pytest test, and run it.
```

The **orchestrator** (`engine/server.py`) runs the agent and holds state; the **client**
drives it from your machine. The recommended client is the **TypeScript terminal client**
in [`clients/ink/`](clients/ink/) — a purpose-built renderer (ghost-free resize, native
scrollback/selection/copy, smooth streaming). `engine/client.py` shown above is the
zero-dependency Python line REPL (the legacy `engine/tui.py` is a full-screen alternative).
Code stays on your machine: the orchestrator's file/command tools are **passed through to
the client and run on YOUR local files**. The agents **plan it, write the code, write the
test, and run it** — you describe, they build. No prior coding needed. Full walkthrough:
**[`docs/self-maintenance.md`](docs/self-maintenance.md)**.

---

Ironclad is a generic, model-agnostic framework for building reliable agentic
systems. It pairs an **Agent-Contract-Kernel (ACK)** — schema-as-single-source-
of-truth, validate→reask→retry, a generator and a preflight doctor — with a
lean **orchestration engine** that turns multi-step agent workflows into
deterministic, fail-closed pipelines.

The guiding principle: a small fast model with hard schema/validation
enforcement beats a large model you "trust" to format its output. You get
**production-grade tool-calling reliability without depending on any specific
model or parser** — the kernel enforces the contract, not the weights.

## 🚧 Status: proven core, redesign landed (pre-release)

Ironclad's engine comes from a **proven, in-production orchestrator** and has now been
**rebuilt**: single process → headless **server + thin client**, containerized, with a
**reasoning-worker fan-out** and a purpose-built **TypeScript terminal client**. The rebuild's pieces are wired **and
tested** — server/client split, a session-gated **secure channel**, governed parallelism,
long-term **memory**, autoplan, and **function-calling robustness** (validate→reask on
every tool call + recovery for models without native tool-calls). On top, the **extension
surface** is shipped: an **open plugin API** (drop in a tool, no core fork), a **bring-your-
own code-agent CLI**, a **dev container** that runs the whole suite as a build+test gate,
and a beginner **self-maintenance** guide. Verified by **236 Python tests** (227 offline + 9
live) plus **272 TypeScript client tests**, and a **full end-to-end run with a real code-agent**.

It is still **pre-release**: single-tenant by design (no multi-user auth yet), no tagged
release, and APIs/layout/config may change. The DEV → Prod → Public **promote pipeline** that
hardens our own releases is **in development** (today it's a manual gated path). The
**TypeScript terminal client** is now **bundled in the repo** (`clients/ink/`,
build-from-source) and is the recommended client. Treat `main` as a development snapshot.

👉 Read these before relying on anything:
- **[`docs/status.md`](docs/status.md)** — honest per-component **wiring status** + load tests.
- **[`docs/test-report.md`](docs/test-report.md)** — what was tested, results, and the
  issues found **and fixed** during the campaign (maximum transparency).
- **[`docs/roadmap.md`](docs/roadmap.md)** — what works today vs what's planned.

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

The recommended TypeScript client over the server/client split — a turn streams live into
the terminal's own scrollback, with a pinned status bar (model · throughput · tasks ·
watcher · connection). Illustrative frame from a real session:

```text
 █▀▄▀█ Ironclad · Orchestrator Client
   Ironclad CLI 0.1.0 · code . · ≤3 agents
  /help · exit · mouse selects/copies natively

 > what is 17 times 23?

 17 times 23 is 391.

 ──────────────────────────────────────────────────────────────────────
 >
 ──────────────────────────────────────────────────────────────────────
 ◆ Ironclad · qwen3.6-35b · ● conn · ○ watch ○ auto · 0P/0IP/0D · 64 tok/s     Developed in the UAE
```

**Reply language is a setting** (`GX10_LANGUAGE` — `en` default, `ar`, `fr`, …). The
model answers in the configured language regardless of the input language. Real output
with `GX10_LANGUAGE=ar`, same question:

```text
 > what is 17 times 23?

 حاصل ضرب 17 في 23 هو 391.
```

`/command` routing (local + forwarded), find-in-buffer (**Ctrl+F**), native
scrollback/selection/copy and **Ctrl+V** paste are built in. Zero-Node Python clients (a
plain line REPL + the legacy full-screen TUI) ship alongside it.

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
to be extended. You extend it over **one open, versioned contract** — the
**[plugin API](docs/plugin-api.md)**: drop a tool into a `skills/` directory, point
`GX10_PLUGINS_DIR` at it, and the agent picks it up **without forking the core** (the
coding CLI itself is swappable too — [bring your own](docs/code-agents.md)). Concrete use
cases also dock on as **vessels** (see [`examples/demo-vessel/`](examples/demo-vessel/); a
generator scaffolds new ones), so a broad audience can build their own domain agents on a
reliable, self-hosted base rather than starting from scratch. The framework is built to
**extend and maintain itself**: its own agents can scaffold new plugins, and a
[dev container](docs/dev-environment.md) runs the full suite as a build+test gate — the
internal **DEV → Prod → Public** promote pipeline that hardens releases is **in
development** (today, a manual gated path). Realistic directions the architecture supports today:

- **Edge & energy efficiency** — a small, enforced model on local/edge hardware
  instead of a large cloud one. That efficiency bet is the whole premise of the project.
- **Education · healthcare · logistics** — build a vessel for your domain: reliable
  tool-using agents and retrieval/RAG assistants over your own data, kept on-prem.

The repo's job is to give you a working starting point, not to ship every vertical —
the verticals are yours to build.

**New here, or not a developer?** AI lets you turn an idea into working software — and
Ironclad's own agents are the on-ramp: you *describe* what you want, they plan it, write
the code, write a test, and run it. Start with
**[`docs/self-maintenance.md`](docs/self-maintenance.md)** — "describe an idea, let the
agents build it" — whether you want to extend Ironclad with a plugin or repurpose the
whole thing for your own project.

## Setup

Requires **Python 3.10+** and an **OpenAI-compatible endpoint** (e.g. vLLM).

**Install the library from PyPI:**

```bash
pip install ironclad-ai          # the ACK library (import ack)
pip install "ironclad-ai[engine]"  # + the orchestration engine deps
```

**Or clone for the full engine + clients** (recommended while pre-release):

```bash
git clone https://github.com/GrokBuildMJW/ironclad.git
cd ironclad
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\Activate.ps1
pip install -e ".[engine]"                         # ACK + engine (openai, prompt_toolkit)

# Point at your model endpoint (defaults: http://localhost:8000/v1, qwen3.6-35b):
export GX10_BASE_URL=http://localhost:8000/v1
export GX10_MODEL=your-served-model-name
export GX10_API_KEY=...                             # only if your endpoint needs one

# the orchestrator + the recommended TypeScript client (install globally, once; needs Node ≥ 22):
python engine/server.py &
( cd clients/ink && npm install && npm install -g . )    # global `ironclad`, like claude / kimi
ironclad --server http://localhost:8100                  # runs in the current folder (codedir = cwd)
# zero-Node alternative: python engine/client.py --codedir .   (legacy TUI: engine/tui.py)
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
ack/                   # Agent-Contract-Kernel: case-spec, validated-emit, registry, doctor, generator
engine/                # orchestration engine: agent loop, task store, fail-closed macros
clients/ink/           # recommended TypeScript terminal client (build-from-source)
examples/demo-vessel   # runnable example workspace
docs/  LICENSE  NOTICE  # guides + Apache-2.0
```

## Roadmap

Honest split of **what works today** vs **what is planned** lives in
**[`docs/roadmap.md`](docs/roadmap.md)**; the per-component wiring status of shipped
features is in **[`docs/status.md`](docs/status.md)**. In short:

- **Today — single-tenant, home-LAN trust.** One operator, one principal, no
  authentication on the server (it trusts its network like the model port). Code stays
  on the client by construction.
- **Phase d (done) — secure, session-gated channel,** still single-operator:
  selectable trust profiles (`open` / `token` / `sealed`), a client-managed tunnel
  option, and an explicit session that **seals on disconnect**. The token is a
  *deployment secret*, not a user login. Built, unit-tested, and **live-verified** over
  a real SSH tunnel on the reference GPU (see [`docs/status.md`](docs/status.md)).
- **Phase e (done) — governed parallelism:** server-side concurrent reasoning the
  orchestrator actually uses (`/fanout` + an in-engine `parallel_reason` tool), made
  GPU-safe by a concurrency cap **and** a token-budget envelope so it can never
  over-subscribe a local box. Conservative defaults in core; model-matched in the deploy.
- **Extend it through itself (shipped surface; promote pipeline in development) —** an
  **open, versioned plugin API** (`GX10_PLUGINS_DIR`), a **bring-your-own code-agent CLI**
  (`GX10_AGENT_CMD`), a **dev container** build+test gate, and a beginner
  **self-maintenance** guide are shipped and tested. The internal three-stage **DEV → Prod →
  Public** promote pipeline that hardens our own releases is **in development** (today a
  manual gated path); the core stays **inbound-closed** (the only inbound is a bug report we
  reproduce → fix → ship).
- **Shipped — the recommended terminal client.** A TypeScript client on a **purpose-built
  renderer** (ghost-free resize, smooth streaming, native-grade scrollback/selection/copy)
  over the same HTTP/tool-bridge contract, now **bundled in the repo**
  (`clients/ink/`, build-from-source) and the recommended interactive UI. The Python
  REPL/TUI remain as **legacy** fallbacks.
- **Planned (Phase g) — Identity & Authorization (multi-tenant):** per-principal scope
  through tasks, memory namespaces, and data-source entitlements, with org/group
  RBAC via OIDC/SAML. **This does not exist yet** — until it lands, treat
  enterprise/government use as single-tenant on trusted infrastructure.
- Also: broader tests, verified recipes for more local open models, **RAG over local
  datasets**, and a **scalable-context memory** layer — a multi-tier system (bounded model
  window + a new **short-term** summary/cache tier + long-term retrieval) with rolling
  summarization and per-turn RAG, so total context exceeds the window while decode stays
  fast (see the roadmap), and a first tagged release.

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
