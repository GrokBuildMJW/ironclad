FROM python:3.11-slim

WORKDIR /app

# curl for the compose healthcheck.
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# CPU torch first (otherwise sentence-transformers pulls the huge CUDA build).
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

# Mem0 0.1.118 with the graph extra (Neo4j) + a local embedder + the API layer.
# Version PINNED: 2.x removed the OSS graph store.
RUN pip install --no-cache-dir \
    "mem0ai[graph]==0.1.118" \
    "sentence-transformers" \
    "fastapi" \
    "uvicorn[standard]"

COPY app.py .

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
