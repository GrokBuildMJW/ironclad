# Reference deployment — NVIDIA DGX Spark

Ironclad is developed and exercised on a single **NVIDIA DGX Spark**. This is the
reference stack: what runs, why, and how to bring it up in one shot. Nothing here is
required by Ironclad — any OpenAI-compatible endpoint works — but reproducing this box
gives you the same behaviour the project is tuned against.

> **Pre-release.** Versions/flags below reflect what currently works on this hardware
> and may change.

## The hardware

- **NVIDIA DGX Spark** — GB10 Grace-Blackwell, compute `sm_121`, **128 GB unified
  memory** (CPU+GPU share one pool).
- Practical consequence: the box is **decode-bandwidth-limited**. A **Mixture-of-
  Experts** model in **NVFP4** (4-bit, runs natively on Blackwell) decodes far faster
  than a dense model of similar quality, and fits comfortably in unified memory. That
  is why the reference model is an **MoE NVFP4**, not a large dense model.

## What runs on the box

| Component        | What it is                                   | Port  | Required for Ironclad |
|------------------|----------------------------------------------|-------|-----------------------|
| **vLLM**         | OpenAI-compatible server, the orchestrator + worker model | 8000  | **Yes** |
| **Orchestrator** | Ironclad server (`engine/server.py`), headless | 8100  | for the server/client split |
| Memory (opt.)    | Mem0 + Qdrant + Neo4j + BGE-M3 long-term memory | 8800 | optional |

The **model**: `RedHatAI/Qwen3.6-35B-A3B-NVFP4` served as `qwen3.6-35b`, on
`vllm/vllm-openai:cu130-nightly`. Key flags and why:

- `--quantization compressed-tensors --kv-cache-dtype fp8` — NVFP4 weights + fp8 KV
  cache to stay within bandwidth/memory.
- `--moe-backend flashinfer_cutlass --attention-backend flashinfer` — the MoE/attn
  kernels that work on `sm_121` (the marlin MoE backend rejects this unquantized path).
- `--gpu-memory-utilization 0.6` — leaves headroom in unified memory for the other
  services (memory stack, orchestrator).
- `--reasoning-parser qwen3 --enable-auto-tool-choice --tool-call-parser qwen3_coder`
  — Qwen3 thinking + native tool calling. Ironclad's ACK emitter turns **thinking off
  per-request** for structured emission (that is what makes it ~100 % reliable);
  ordinary chat keeps thinking on.
- **CUDA 13 nightly image matters:** grammar/constrained decoding (XGrammar) crashed
  the engine on older images on this GPU; it works on `cu130-nightly`.

## One-shot setup

On the Spark, from a checkout of this repo:

```bash
# 1) (once) get the weights with the Hugging Face CLI (`pip install -U huggingface_hub`):
#    hf download RedHatAI/Qwen3.6-35B-A3B-NVFP4 \
#       --local-dir ~/models/RedHatAI-Qwen3.6-35B-A3B-NVFP4

# 2) one-shot: vLLM + orchestrator
bash scripts/spark-bootstrap.sh \
     --model-dir ~/models/RedHatAI-Qwen3.6-35B-A3B-NVFP4 \
     --served-name qwen3.6-35b \
     --with-orchestrator
```

The script is **idempotent** (re-run safe), parameterized (no host/IP baked in), and
waits for `/v1/models` (and `/health` if `--with-orchestrator`) before returning. See
`bash scripts/spark-bootstrap.sh --help` for all flags.

**Orchestrator as a container.** Add `--docker` to run the orchestrator as a service
(built from [`Dockerfile`](../Dockerfile)) instead of a venv+tmux process — it joins
vLLM (and the memory stack) as a managed container:

```bash
bash scripts/spark-bootstrap.sh --model-dir <weights> --served-name qwen3.6-35b \
     --with-orchestrator --docker
```

**One compose for the whole stack.** [`docker-compose.yml`](../docker-compose.yml) can
bring up the model **and** the orchestrator together:

```bash
# model + orchestrator (needs a GPU + the NVIDIA container runtime):
IRONCLAD_MODEL_DIR=~/models/RedHatAI-Qwen3.6-35B-A3B-NVFP4 docker compose --profile model up -d

# orchestrator only (you already run a model):
GX10_MODEL=qwen3.6-35b docker compose up -d
```

The orchestrator waits for vLLM's healthcheck before starting; host networking lets it
reach vLLM on `localhost:8000` and bind `:8100`; the workspace persists in
`ironclad-workdir/`. Long-term memory (Mem0) stays external — set `GX10_MEMORY_URL`.

Then, from your workstation — the recommended TypeScript client (install it once with
`npm install && npm install -g .` in `clients/ink/` → a global `ironclad`), or the
zero-dependency Python REPL:

```bash
GX10_SERVER_URL=http://<spark-host>:8100 ironclad
# zero-Node alternative: GX10_SERVER_URL=http://<spark-host>:8100 python engine/client.py --codedir .
```

## Optional: long-term memory stack

Ironclad runs fine without it. If you want persistent agent memory, the compose ships
a **Mem0** service (vector + graph) under the `memory` profile:

```bash
NEO4J_PASSWORD=change-me GX10_MEMORY_URL=http://localhost:8800 \
  docker compose --profile model --profile memory up -d
```

This brings up **Qdrant** (vectors) + **Neo4j** (graph) + a **Mem0 API** with **BGE-M3**
embeddings (built from [`memory-service/`](../memory-service/)), with Mem0's LLM pointed
back at the local model. Setting `GX10_MEMORY_URL` makes the orchestrator use it
(`/health` then reports `memory: up`).

Notes: `mem0ai==0.1.118` is pinned with the `[graph]` extra (later 2.x dropped the OSS
graph store); the read path is vector-only by default (`graph=false`) — enable graph
only for relational queries, it's slower. The store starts **empty**; it accumulates
from task completions (any pre-existing corpus is yours to import).

## Verify

```bash
curl -s http://localhost:8000/v1/models          # model listed
curl -s http://localhost:8100/health             # {"ok": true, "model": "qwen3.6-35b", ...}
```

## Notes / gotchas observed on this hardware

- **Cold start** loads weights into VRAM up front — the first `/v1/models` can take a
  few minutes.
- **Concurrency:** vLLM batches `--max-num-seqs` (8) concurrent sequences — Ironclad's
  `/fanout` exploits this for parallel reasoning (measured ~5.8× over serial).
- **Large-context prefill:** for streaming OpenAI-compatible calls, the HTTP read timeout
  also bounds time-to-first-token because vLLM emits no body bytes during prefill. A
  roughly 64k-context request can exceed the public 120s default on this hardware while
  the model is healthy. For Spark deployments, set `connection.first_token_timeout_s`
  generously and keep it above `context.turn_idle_timeout_s`; keep
  `connection.connect_timeout_s` short so dead endpoints still fail fast.
- **One model at a time:** with 128 GB unified, run a single NVFP4 MoE plus the small
  services; don't try to co-host a second large model.
