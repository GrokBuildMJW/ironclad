# Setup

> **Status: pre-release / in active development.** No tagged release yet; expect
> change. Ironclad is developed and tested on an **NVIDIA DGX Spark** (GB10,
> Blackwell `sm_121`, 128 GB unified memory) with a local **vLLM** server running
> **Qwen3.6-35B-A3B-NVFP4**. Any OpenAI-compatible endpoint works; the defaults just
> match that reference box.

Prefer to have an AI coding agent do this for you? See **[`AGENTS.md`](AGENTS.md)**.

---

## 1. Prerequisites

- **Python 3.10+**
- An **OpenAI-compatible chat endpoint** (vLLM, llama.cpp server, vLLM-OpenAI, etc.)
  reachable over HTTP. The endpoint must support tool/function calling for the
  orchestration engine; the ACK library alone only needs the endpoint for emission.
- Optional: **`prompt_toolkit`** for the full-screen TUI (installed by the `[engine]`
  extra). Without it the client falls back to a plain line REPL.

## 2. Install

```bash
git clone https://github.com/GrokBuildMJW/ironclad.git
cd ironclad
python -m venv .venv
. .venv/bin/activate                 # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -e ".[engine]"           # ACK (pydantic) + engine extras (openai, prompt_toolkit)
```

`pip install -e .` (without the extra) installs just the **ACK library** (`import ack`)
— pydantic-only, for embedding the contract kernel in your own app.

## 3. Configure the model endpoint

Configuration precedence (low → high): **code defaults → config file/dir → `GX10_*`
env vars → CLI flags**. The simplest path is env vars:

| Variable          | Meaning                                  | Default                     |
|-------------------|------------------------------------------|-----------------------------|
| `GX10_BASE_URL`   | OpenAI-compatible base URL               | `http://localhost:8000/v1`  |
| `GX10_MODEL`      | served model name                        | `qwen3.6-35b`               |
| `GX10_API_KEY`    | API key (only if your endpoint needs one)| `not-needed`                |
| `GX10_WORKDIR`    | workspace (tasks/, summaries/, session)  | `.`                         |
| `GX10_MAX_TOKENS` | output token cap                         | `8192`                      |
| `GX10_THINKING`   | `auto` \| `first` \| `off` \| `all`      | `auto`                      |

Or drop a `gx10.config.json` next to where you run it:

```json
{
  "connection": { "base_url": "http://localhost:8000/v1", "model": "your-model",
                  "api_key_env": "GX10_API_KEY" },
  "generation": { "max_tokens": 8192, "thinking_mode": "auto" },
  "paths":      { "workdir": "./my-workspace" }
}
```

## 4. Run

### A. Monolithic CLI (one process)

Everything in one full-screen process, talking straight to your endpoint:

```bash
python engine/gx10.py --workdir ./my-workspace
```

### B. Server / client split

The orchestrator (reasoning + state) runs next to the model; a thin client runs where
your code lives. Plain LAN HTTP, client-initiated — so project code never leaves your
machine and the code-agents run locally.

```bash
# On the model box (next to vLLM):
python engine/server.py --host 0.0.0.0 --port 8100

# On your machine — full-screen TUI (old look-and-feel, live streaming):
GX10_SERVER_URL=http://<server-host>:8100 python engine/tui.py --codedir .

# ...or the plain line REPL:
GX10_SERVER_URL=http://<server-host>:8100 python engine/client.py --codedir .
```

Server endpoints: `GET /health /tasks /pending` · `POST /chat /chat/stream /feedback
/fanout`. The client pulls staged handovers from `/pending`, runs the code-agents
locally, and posts results back to `/feedback`. `--max-agents` bounds how many run in
parallel.

## 5. Reference vLLM launch (DGX Spark)

> **One-shot:** on a DGX Spark, instead of the manual command below, run
> `bash scripts/spark-bootstrap.sh --model-dir <weights> --served-name qwen3.6-35b
> --with-orchestrator` (idempotent, waits for readiness). Full reference stack and
> rationale: [`docs/dgx-spark.md`](docs/dgx-spark.md).

The endpoint Ironclad is developed against — a single NVFP4 MoE on one GB10. Adjust
paths/flags to your box; nothing here is required by Ironclad:

```bash
docker run -d --name vllm --restart unless-stopped --gpus all --ipc host \
  -v ~/models:/models -p 8000:8000 \
  vllm/vllm-openai:cu130-nightly \
  /models/RedHatAI-Qwen3.6-35B-A3B-NVFP4 \
  --served-model-name qwen3.6-35b \
  --quantization compressed-tensors --kv-cache-dtype fp8 \
  --attention-backend flashinfer --moe-backend flashinfer_cutlass \
  --tensor-parallel-size 1 --gpu-memory-utilization 0.6 \
  --max-model-len 32768 --max-num-seqs 8 \
  --enable-chunked-prefill --enable-prefix-caching \
  --reasoning-parser qwen3 --enable-auto-tool-choice --tool-call-parser qwen3_coder
```

Notes from this hardware: GB10 is decode-bandwidth-limited, so an **MoE in NVFP4**
beats a dense model; structured/constrained decoding (XGrammar) works on the **CUDA 13
nightly** image but crashed on older ones; per-request **thinking-off** is what makes
structured emission reliable (the ACK emitter sets it; normal chat keeps thinking on).

## 6. Verify

```bash
pip install pytest
python -m pytest ack/tests -q          # contract kernel + engine + split tests

# Smoke the endpoint wiring (expects your model to answer):
python engine/gx10.py --workdir ./my-workspace --thinking off
# then type:  was ist 17 mal 23?
```

## 7. Troubleshooting

- **`prompt_toolkit` missing / "NoConsoleScreenBufferError".** Install the `[engine]`
  extra, and run the TUI in a real terminal (PowerShell / Windows Terminal), not a
  bare pipe. The client auto-falls back to the line REPL if prompt_toolkit is absent.
- **`UnicodeEncodeError` (cp1252) on Windows.** Run with `PYTHONIOENCODING=utf-8`.
- **Engine can't reach the model.** Check `GX10_BASE_URL`/`GX10_MODEL` and that your
  endpoint serves `/v1/models`.
- **Constrained decoding crashes the server.** Some vLLM/GPU combos can't run the
  grammar bitmask kernel; use a recent vLLM build, or rely on the soft validate→reask
  path (ACK works either way).
