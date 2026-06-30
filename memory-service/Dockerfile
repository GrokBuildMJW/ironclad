FROM python:3.11-slim

WORKDIR /app

# curl for the compose healthcheck.
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# CPU torch first (otherwise sentence-transformers pulls the huge CUDA build).
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

# Mem0 0.1.118 with the graph extra (Neo4j) + a local embedder + the API layer.
# Version PINNED: 2.x removed the OSS graph store.
# qdrant-client: the /scopes payload scroll (#601 S15). redis: the optional per-mem_ns reflection
# counter+lock backend (#634; Valkey speaks the RESP protocol — fail-soft when MEM0_WARM_URL is unset).
RUN pip install --no-cache-dir \
    "mem0ai[graph]==0.1.118" \
    "sentence-transformers" \
    "fastapi" \
    "uvicorn[standard]" \
    "qdrant-client" \
    "redis"

# COPY every module app.py imports (scope_guard #601 S15, reflect_policy MEMSVC-1, curate #634) — a
# `COPY app.py` alone fails the build at import (the deployed image predated these, so it never surfaced).
COPY app.py scope_guard.py reflect_policy.py curate.py ./

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
