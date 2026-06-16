#!/usr/bin/env bash
# Ironclad — one-shot DGX Spark bootstrap (reference stack).
#
# Brings up the model server (vLLM) and, optionally, the Ironclad orchestrator
# server — idempotent, re-runnable. Parameterized: pass host-specific values as
# flags / env, NOTHING is baked in (the repo stays secret-free).
#
# What it sets up (see docs/dgx-spark.md for the why):
#   1. vLLM (OpenAI-compatible) serving an NVFP4 MoE on the GB10 GPU, port 8000.
#   2. (optional) the Ironclad orchestrator server in a venv, port 8100.
#
# Prereqs (present on a stock DGX Spark / DGX OS): Docker with the NVIDIA runtime
# (`--gpus all`), Python 3.10+, and the model weights already on disk.
#
# Usage:
#   bash scripts/spark-bootstrap.sh --model-dir ~/models/RedHatAI-Qwen3.6-35B-A3B-NVFP4 \
#                                   --served-name qwen3.6-35b [--with-orchestrator] [--docker]
#
#   --with-orchestrator   also start the Ironclad orchestrator server (port 8100)
#   --docker              run the orchestrator as a container (build core/Dockerfile)
#                         instead of a venv+tmux process; requires --with-orchestrator
set -euo pipefail

# ── defaults (override via flags or GX10_* env) ───────────────────────────────
MODEL_DIR="${IRONCLAD_MODEL_DIR:-$HOME/models/RedHatAI-Qwen3.6-35B-A3B-NVFP4}"
SERVED_NAME="${GX10_MODEL:-qwen3.6-35b}"
VLLM_IMAGE="${IRONCLAD_VLLM_IMAGE:-vllm/vllm-openai:cu130-nightly}"
VLLM_PORT="${IRONCLAD_VLLM_PORT:-8000}"
GPU_MEM_UTIL="${IRONCLAD_GPU_MEM_UTIL:-0.6}"
MAX_MODEL_LEN="${IRONCLAD_MAX_MODEL_LEN:-32768}"
MAX_SEQS="${IRONCLAD_MAX_SEQS:-8}"
WITH_ORCH=0
ORCH_DOCKER=0
ORCH_PORT="${GX10_SERVER_PORT:-8100}"
CONTAINER="${IRONCLAD_VLLM_CONTAINER:-vllm}"
ORCH_CONTAINER="${IRONCLAD_ORCH_CONTAINER:-ironclad-orchestrator}"
# Workspace volume OUTSIDE the synced repo: the container writes it as root, so it
# must not sit under core/ (a re-sync's `rm -rf core` would hit permission errors).
ORCH_WORKDIR="${IRONCLAD_WORKDIR:-$HOME/ironclad-workdir}"

while [ $# -gt 0 ]; do
  case "$1" in
    --model-dir)         MODEL_DIR="$2"; shift 2;;
    --served-name)       SERVED_NAME="$2"; shift 2;;
    --vllm-image)        VLLM_IMAGE="$2"; shift 2;;
    --port)              VLLM_PORT="$2"; shift 2;;
    --gpu-mem-util)      GPU_MEM_UTIL="$2"; shift 2;;
    --with-orchestrator) WITH_ORCH=1; shift;;
    --docker)            ORCH_DOCKER=1; shift;;
    --orchestrator-port) ORCH_PORT="$2"; shift 2;;
    -h|--help)
      sed -n '2,20p' "$0"; exit 0;;
    *) echo "unknown flag: $1 (try --help)"; exit 2;;
  esac
done

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # scripts/ → repo root

log() { echo "  [bootstrap] $*"; }
fail() { echo "  [bootstrap] FAIL: $*" >&2; exit 1; }

# ── preflight ────────────────────────────────────────────────────────────────
command -v docker >/dev/null || fail "docker not found"
[ -e "$MODEL_DIR" ] || fail "model dir not found: $MODEL_DIR (download the weights first)"
docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q nvidia \
  || log "warn: NVIDIA docker runtime not detected — '--gpus all' may fail"

# ── 1) vLLM ──────────────────────────────────────────────────────────────────
if curl -fs "http://localhost:${VLLM_PORT}/v1/models" >/dev/null 2>&1; then
  log "an OpenAI endpoint already responds on :${VLLM_PORT} — leaving the model server as is"
elif [ "$(docker ps -q -f name="^${CONTAINER}$")" ]; then
  log "vLLM container '${CONTAINER}' already running — leaving as is"
else
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  log "starting vLLM ($VLLM_IMAGE) serving '$SERVED_NAME' on :$VLLM_PORT"
  docker run -d --name "$CONTAINER" --restart unless-stopped --gpus all --ipc host \
    -v "$MODEL_DIR":/model -p "${VLLM_PORT}":8000 \
    "$VLLM_IMAGE" /model \
    --served-model-name "$SERVED_NAME" \
    --quantization compressed-tensors --kv-cache-dtype fp8 \
    --attention-backend flashinfer --moe-backend flashinfer_cutlass \
    --tensor-parallel-size 1 --gpu-memory-utilization "$GPU_MEM_UTIL" \
    --max-model-len "$MAX_MODEL_LEN" --max-num-seqs "$MAX_SEQS" \
    --enable-chunked-prefill --enable-prefix-caching \
    --reasoning-parser qwen3 --enable-auto-tool-choice --tool-call-parser qwen3_coder \
    >/dev/null
fi

log "waiting for vLLM /v1/models (cold start pulls weights into VRAM, can take minutes)…"
for i in $(seq 1 120); do
  if curl -fs "http://localhost:${VLLM_PORT}/v1/models" >/dev/null 2>&1; then
    log "vLLM is up on :$VLLM_PORT"; break
  fi
  [ "$i" = 120 ] && fail "vLLM did not become ready — check: docker logs $CONTAINER"
  sleep 5
done

# ── 2) orchestrator (optional) ───────────────────────────────────────────────
if [ "$WITH_ORCH" = 1 ] && [ "$ORCH_DOCKER" = 1 ]; then
  # Containerisiert: Image bauen + als Service neben vLLM laufen lassen (host-net,
  # damit localhost:VLLM_PORT erreichbar ist und :ORCH_PORT auf dem Host bindet).
  log "building orchestrator image '$ORCH_CONTAINER' from $REPO_ROOT"
  docker build -t "$ORCH_CONTAINER" "$REPO_ROOT" >/dev/null
  docker rm -f "$ORCH_CONTAINER" >/dev/null 2>&1 || true
  mkdir -p "$ORCH_WORKDIR"
  docker run -d --name "$ORCH_CONTAINER" --restart unless-stopped --network host \
    -e GX10_BASE_URL="http://localhost:${VLLM_PORT}/v1" \
    -e GX10_MODEL="$SERVED_NAME" -e GX10_SERVER_PORT="$ORCH_PORT" -e GX10_WORKDIR=/work \
    -e GX10_LANGUAGE="${GX10_LANGUAGE:-en}" \
    -v "$ORCH_WORKDIR":/work "$ORCH_CONTAINER" >/dev/null
  log "orchestrator container '$ORCH_CONTAINER' on :$ORCH_PORT"
  for i in $(seq 1 30); do
    curl -fs "http://localhost:${ORCH_PORT}/health" >/dev/null 2>&1 && { log "orchestrator healthy"; break; }
    [ "$i" = 30 ] && fail "orchestrator container not healthy — check: docker logs $ORCH_CONTAINER"
    sleep 1
  done
elif [ "$WITH_ORCH" = 1 ]; then
  VENV="$REPO_ROOT/.venv"
  [ -d "$VENV" ] || python3 -m venv "$VENV"
  # shellcheck disable=SC1091
  . "$VENV/bin/activate"
  pip install --quiet --upgrade pip
  pip install --quiet -e "$REPO_ROOT[engine]" 2>/dev/null || pip install --quiet openai pydantic
  export GX10_BASE_URL="http://localhost:${VLLM_PORT}/v1"
  export GX10_MODEL="$SERVED_NAME"
  export GX10_LANGUAGE="${GX10_LANGUAGE:-en}"
  if command -v tmux >/dev/null; then
    tmux kill-session -t ironclad 2>/dev/null || true
    tmux new-session -d -s ironclad -c "$REPO_ROOT" \
      "GX10_BASE_URL=$GX10_BASE_URL GX10_MODEL=$GX10_MODEL \
       python engine/server.py --host 0.0.0.0 --port $ORCH_PORT"
    log "orchestrator in tmux session 'ironclad' on :$ORCH_PORT"
  else
    nohup python "$REPO_ROOT/engine/server.py" --host 0.0.0.0 --port "$ORCH_PORT" \
      >"$REPO_ROOT/orchestrator.log" 2>&1 &
    log "orchestrator (nohup) on :$ORCH_PORT — log: orchestrator.log"
  fi
  for i in $(seq 1 20); do
    curl -fs "http://localhost:${ORCH_PORT}/health" >/dev/null 2>&1 && { log "orchestrator healthy"; break; }
    [ "$i" = 20 ] && fail "orchestrator did not become healthy on :$ORCH_PORT"
    sleep 1
  done
fi

log "done. vLLM: http://localhost:${VLLM_PORT}/v1  ($SERVED_NAME)"
[ "$WITH_ORCH" = 1 ] && log "orchestrator: http://localhost:${ORCH_PORT}  (connect a client with GX10_SERVER_URL)"
echo "  [bootstrap] one-shot complete."
