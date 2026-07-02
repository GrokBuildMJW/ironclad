# Ironclad

[![CI](https://github.com/GrokBuildMJW/ironclad/actions/workflows/ci.yml/badge.svg)](https://github.com/GrokBuildMJW/ironclad/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/ironclad-ai)](https://pypi.org/project/ironclad-ai/)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Node](https://img.shields.io/badge/Node-%E2%89%A522-3c873a?logo=node.js&logoColor=white)
![Status](https://img.shields.io/badge/status-pre--release-orange)
[![Stars](https://img.shields.io/github/stars/GrokBuildMJW/ironclad?style=social)](https://github.com/GrokBuildMJW/ironclad/stargazers)

**Your own agentic platform — self-hosted, model-agnostic, reliable by contract.**
No vendor lock-in, no surprise limits, no subscription. Run it on your hardware with any
open model; reliability comes from **enforced contracts, not model size**.

Ironclad is a generic framework for building reliable agentic systems that you **fully own
and host yourself**. It exists to keep you **independent of proprietary providers** — no
sudden rate changes or feature removals, no forced subscription, no data leaving your
infrastructure. It pairs an **Agent-Contract-Kernel (ACK)** — schema-as-single-source-of-
truth, validate→reask→retry, a generator and a preflight doctor — with a lean
**orchestration engine** that turns multi-step agent workflows into deterministic,
fail-closed pipelines. The guiding principle: a small, fast, *self-hosted* model under hard
schema enforcement beats a large proprietary one you merely *trust* to format its output —
and it **learns from its own runs** and stays **yours**.

## Why Ironclad

- **Independent & self-hosted.** Any OpenAI-compatible endpoint (vLLM, …); your box, your
  model, your data. No cloud dependency, no lock-in, no subscription — immune to a vendor's
  sudden limits or pricing changes.
- **Model-agnostic & standalone.** Swap the orchestrator model freely; reliability comes
  from the kernel, not the weights. No hidden dependency on any private deployment.
- **Contract-first, fail-closed.** One Pydantic schema drives the prompt, the validator,
  the docs and (where the hardware allows) constrained decoding; macro steps do the
  mechanical file work deterministically in code — fewer round-trips, no silent
  half-completions.
- **Yours to extend.** One open, versioned plugin contract, a bring-your-own coding-agent
  CLI, and a framework that can scaffold and maintain itself.

## Quickstart — describe an idea, let the agents build it

```bash
git clone https://github.com/GrokBuildMJW/ironclad.git && cd ironclad
pip install -e ".[engine]"
export GX10_BASE_URL=http://localhost:8000/v1 GX10_MODEL=your-model   # your model endpoint

python engine/server.py &              # 1) the orchestrator (the agent + state)
python engine/client.py --codedir .    # 2) the client — drives it; type what you want
#   /project new demo --type software      ← create a workspace first (artefacts live under vault/<slug>/)
#   Read README.md and summarise it.   /   Build add(a,b) in calc.py with a pytest test, and run it.
```

The **orchestrator** (`engine/server.py`) runs the agent and holds state; the **client**
drives it from your machine and **keeps your code local** — the orchestrator's file/command
tools are passed through to the client and run on YOUR local files. The agents **plan it,
write the code, write the test, and run it** — you describe, they build; no prior coding
needed.

> **One step first:** any work that produces artefacts (a task, a handover, an MPR run)
> needs an active **project** — `/project new <name>` (`/initiative` is a
> deprecated alias). It is fail-closed: without one the macros refuse rather than scatter
> state into your project root. Plain Q&A turns need no project. See
> [`docs/project-isolation.md`](docs/project-isolation.md) and
> [`docs/state-and-initiative.md`](docs/state-and-initiative.md).

## What you get

- **Headless server + thin client** — the orchestrator runs as a service; clients drive it
  over plain HTTP and keep code local. Recommended client is the **TypeScript terminal
  client** ([`clients/ink/`](clients/ink/)) on a purpose-built renderer (ghost-free resize,
  smooth streaming, native-grade scrollback/selection/copy). Zero-dependency Python clients
  (line REPL + legacy full-screen TUI) ship alongside it.
- **Reliable tool-calling** — the ACK validates every tool call (validate→reask) and
  recovers for models without native tool-calls, so structured output doesn't depend on a
  specific model or parser.
- **Loop Intelligence — an always-on, self-improving context loop (ACE).** The engine learns
  across runs into an evolving, itemized **playbook** of bullets, refined by
  Generator→Reflector→Curator with incremental delta updates + grow-and-refine
  ([Agentic Context Engineering](https://arxiv.org/abs/2510.04618)). It is the **always-on
  core mechanic** — no enable flag — registered as the lesson provider and superseding the
  earlier string-lesson layer. A mark-only **Verifier** scores each handover and a **Quality
  circuit-breaker** trips on sustained degradation (both ride a dependency-inverted
  **Hook-Bus** over agent-loop events, never relaxing the fail-closed core). The loop also
  **learns from the dev-process itself** — the transition ledger both dev-processes emit is
  distilled into per-unit lessons and correlated to the bullets each handover used. And at an
  **architecture fork** it can (gated `ace.fork_mpr.enabled`, default off) run the bundled
  **MPR** multi-perspective panel off the hot path to attach a decision-matrix as a
  *recommendation* to the human ask, then learn the chosen outcome so the next comparable fork
  is pre-informed — always proposing, never deciding.
- **Governed parallelism + provider routing** — server-side reasoning fan-out
  (`/fanout` + the in-engine `parallel_reason` tool), GPU-safe via a concurrency cap and a
  token-budget envelope; a **provider router/dispatcher** on top routes work across
  substrates with fail-soft spill (off by default).
- **Scalable-context memory** — long-term vector(+graph) store plus a multi-tier context
  system (bounded model window + short-term summary/cache tier + long-term retrieval) with
  rolling summarization and per-turn RAG.
- **Web search, trust-gated** — a first-class `web_search` tool over a vendor-neutral
  adapter seam (a CLI delegate or native HTTP), kept out of the `sealed` profile by default
  (operator opt-in); results stream a `web N · Xms` chip to the client. See
  [`docs/web-search.md`](docs/web-search.md).
- **Built-in prompt & skill library** — reusable prompt items and typed skills ship in the
  box (including the bundled **MPR** multi-perspective reasoner as one example skill);
  discover them with `/prompts` / `/skills` and run a prompt directly as `/<prompt-name>`
  (e.g. `/code-review`), or scaffold your own with the paved-road generator.
- **Secure, session-gated channel** — selectable trust profiles (`open` / `token` /
  `sealed`) with an explicit session that seals on disconnect (single-operator).
- **An open extension surface** — a versioned **[plugin API](docs/plugin-api.md)** (no core
  fork) and a **[bring-your-own code-agent CLI](docs/code-agents.md)**: extend the platform
  without forking the core.

It's a natural reliability layer for **regional open models** too — point it at Falcon,
Jais or K2 Think via vLLM ([running on other models](docs/models/)) and get fail-closed
pipelines and structured tool-calls without forking or retraining anything.

## Status & honesty (pre-release)

Ironclad's engine comes from a **proven, in-production orchestrator**, now **rebuilt** into
the server + client architecture above and **wired and tested**. It is still
**pre-release** (0.0.x, alpha): single-tenant by design (no multi-user auth yet) and
APIs/layout/config may change. Tagged releases ship on **PyPI** (`ironclad-ai`) and as
**GitHub Releases** (currently `v0.0.23`) — treat them as early previews and `main` as a
development snapshot. The internal DEV → Prod → Public **promote pipeline** that hardens our
releases is in development (today a manual gated path).

Verified by **2497 Python tests** (2488 offline + 9 live) plus **367 TypeScript client
tests**, and a **full end-to-end run with a real code-agent**. Read these before relying on
anything:

- **[`docs/status.md`](docs/status.md)** — honest per-component **wiring status** (what runs now) + load tests.
- **[`docs/test-report.md`](docs/test-report.md)** — what was tested, results, and the
  issues found **and fixed** during the campaign.
- **[`docs/roadmap.md`](docs/roadmap.md)** — what's **planned or in progress** (future only).
- **[`docs/command-ergonomics.md`](docs/command-ergonomics.md)** — aliases, did-you-mean, argument autocomplete, discovery, and confirm-before-destructive for the slash-command surface.
- **[`docs/docs-guide.md`](docs/docs-guide.md)** — how the docs are organised (one responsibility per doc).

## Reference environment & benchmarks

Developed and exercised on an **NVIDIA DGX Spark** (GB10, Blackwell `sm_121`, 128 GB unified
memory) running a local **vLLM** server with **Qwen3.6-35B-A3B-NVFP4**. Nothing is
hard-wired to that box — any OpenAI-compatible endpoint works — but the defaults
(`localhost:8000`, `qwen3.6-35b`), the throughput numbers and the constrained-decoding
findings reflect that hardware. See [`docs/dgx-spark.md`](docs/dgx-spark.md) for the full
reference stack and a one-shot bootstrap (`scripts/spark-bootstrap.sh`).

| Workload | Result |
|----------|--------|
| Reasoning **fan-out**, 8 independent prompts | **5.8× faster** than serial (1.2 s vs 7.1 s), ~118 tok/s aggregate |
| Conversational turn (single agent) | ~55–68 tok/s, ~2.1 s mean latency |
| Structured emission (ACK, thinking-off) | 100% schema-valid in measurement |

Numbers scale with the model and GPU; reproduce with your own endpoint. Full method and the
per-component wiring status live in [`docs/status.md`](docs/status.md).

## Demo

The recommended TypeScript client streams a turn live into the terminal's own scrollback,
with a pinned status bar (model · connection · memory · warm · watcher · autopilot ·
tasks · coder · web search · throughput):

```text
 █▀▄▀█ Ironclad · Orchestrator Client
   Ironclad CLI 0.1.0 · code . · ≤3 agents
  /help · exit

 > what is 17 times 23?

 17 times 23 is 391.
 ──────────────────────────────────────────────────────────────────────
 ◆ Ironclad · qwen3.6-35b · ● conn · ○ watch ○ auto · 0P/0IP/0D · 64 tok/s
```

**Reply language is a setting** (`GX10_LANGUAGE` — `en` default, `ar`, `fr`, …): the model
answers in the configured language regardless of input language. `/command` routing,
find-in-buffer (**Ctrl+F**), native scrollback/selection/copy and **Ctrl+V** paste are
built in.

## Extend it — a starting point to build on

Ironclad is a **foundation, not a finished product**. You extend it over one open, versioned
contract — the **[plugin API](docs/plugin-api.md)**: drop a tool into a `skills/` directory,
point `GX10_PLUGINS_DIR` at it, and the agent picks it up **without forking the core**. Building
in your **own repo**? `pip install ironclad-ai` and import the curated **[Extension SDK](docs/adr/0004-extension-sdk.md)**
(`ack.sdk`) to validate and ship plugins against a versioned surface.
Concrete use cases dock on as **vessels** (see
[`examples/demo-vessel/`](examples/demo-vessel/); a generator scaffolds new ones), so you
build domain agents on a reliable, self-hosted base instead of starting from scratch. The
framework even **maintains itself** — its agents scaffold new plugins, and a
[dev container](docs/dev-environment.md) runs the full suite as a build+test gate.

Directions the architecture supports today:

- **Edge & energy efficiency** — a small, enforced model on local/edge hardware instead of
  a large cloud one. That efficiency bet is the premise of the project.
- **Education · healthcare · logistics** — build a vessel for your domain: reliable
  tool-using agents and retrieval/RAG assistants over your own data, kept on-prem.

**New here, or not a developer?** You *describe* what you want and Ironclad's own agents
plan it, write the code, write a test, and run it. Start with
**[`docs/self-maintenance.md`](docs/self-maintenance.md)** — extend Ironclad with a plugin,
or repurpose the whole thing for your own project.

## Setup

Requires **Python 3.10+** and an **OpenAI-compatible endpoint** (e.g. vLLM).

**Install the library from PyPI:**

```bash
pip install ironclad-ai             # the ACK library (import ack)
pip install "ironclad-ai[engine]"  # + the orchestration engine deps
```

**Or clone + one-shot install** (recommended while pre-release) — builds the venv, engine, client, config
and an `ironclad` command in a single command (cross-platform, secret-free; endpoints default to localhost):

```bash
git clone https://github.com/GrokBuildMJW/ironclad.git && cd ironclad
bash install/ironclad-install.sh        # Windows: install\ironclad-install.ps1   (override: --base-url/--model)
source ~/.bashrc && ironclad            # Windows: . $PROFILE ; ironclad
```

See [`install/README.md`](install/README.md) for flags + the doctor.

**Or wire it manually:**

```bash
git clone https://github.com/GrokBuildMJW/ironclad.git
cd ironclad
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\Activate.ps1
pip install -e ".[engine]"

# Point at your model endpoint (defaults: http://localhost:8000/v1, qwen3.6-35b):
export GX10_BASE_URL=http://localhost:8000/v1
export GX10_MODEL=your-served-model-name
export GX10_API_KEY=...                           # only if your endpoint needs one

# the orchestrator + the recommended TypeScript client (needs Node ≥ 22):
python engine/server.py &
( cd clients/ink && npm install && npm install -g . )    # global `ironclad`, like claude / kimi
ironclad --server http://localhost:8100                  # runs in the current folder (codedir = cwd)
# then, before the first build/task:  /project new myproject --type software
# zero-Node alternative: python engine/client.py --codedir .   (legacy TUI: engine/tui.py)
```

- **Full walkthrough, the server/client split, and the reference vLLM launch:** see
  **[`SETUP.md`](SETUP.md)** — including copy-paste **shell shortcuts** for Windows
  PowerShell, macOS and Linux.
- **Let an AI coding agent set it up for you** (deterministic, verifiable runbook): see
  **[`AGENTS.md`](AGENTS.md)**.

A runnable **demo vessel** lives in [`examples/demo-vessel/`](examples/demo-vessel/) — a
minimal workspace showing a contract spec, a pipeline, and the doctor preflight.

## Layout

```
ack/                   # Agent-Contract-Kernel: case-spec, validated-emit, registry, doctor, generator
engine/                # orchestration engine: agent loop, task store, fail-closed macros
clients/ink/           # recommended TypeScript terminal client (build-from-source)
examples/demo-vessel   # runnable example workspace
docs/  LICENSE  NOTICE  # guides + Apache-2.0
```

At runtime, in your **workdir**, state stays out of the project root: engine machinery is
hidden under `.ironclad/` (session, warm-cache, the active-initiative marker) and every
produced artefact lives under `vault/<slug>/` — see
[`docs/state-and-initiative.md`](docs/state-and-initiative.md).

## Roadmap

What's **planned or in progress** lives in **[`docs/roadmap.md`](docs/roadmap.md)** (future
only); what **runs today**, per component, is in **[`docs/status.md`](docs/status.md)**. In
short: today is **single-tenant, home-LAN trust** (one operator, code stays on the client);
multi-tenant **identity & authorization** does not exist yet — until it lands, treat
enterprise/government use as single-tenant on trusted infrastructure.

Issues and discussions are welcome — this is an early, openly-developed project.

## License

Apache License 2.0 — see [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).
Copyright 2026 MJWC-AI-LAB and Ironclad contributors.

---

🇦🇪 Built in the United Arab Emirates by **MJWC-AI-LAB**.
