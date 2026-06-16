# Ironclad orchestrator server — headless, OpenAI-compatible-endpoint client.
# Multi-arch (works on the DGX Spark's arm64/GB10). Secret-free: all connection
# details come from env at run time, nothing baked in.
FROM python:3.12-slim

WORKDIR /app

# Runtime deps only: the server talks to vLLM (openai) and validates with pydantic.
# prompt_toolkit is NOT needed server-side (that's the client/TUI).
RUN pip install --no-cache-dir "openai>=1" "pydantic>=2"

# Source (engine + the ACK package it imports). gx10.py puts /app on sys.path so
# `import ack` resolves; server.py adds engine/ for `import gx10`.
COPY ack ./ack
COPY engine ./engine
COPY pyproject.toml LICENSE NOTICE README.md ./

ENV PYTHONIOENCODING=utf-8 \
    PYTHONUNBUFFERED=1 \
    GX10_WORKDIR=/work \
    GX10_BASE_URL=http://localhost:8000/v1 \
    GX10_MODEL=qwen3.6-35b \
    GX10_SERVER_PORT=8100

RUN mkdir -p /work
EXPOSE 8100

# Autopilot stays off server-side (the client launches code-agents); the server
# only reasons + holds state. Override host/port/url via env or compose.
CMD ["sh", "-c", "python engine/server.py --host 0.0.0.0 --port ${GX10_SERVER_PORT}"]
