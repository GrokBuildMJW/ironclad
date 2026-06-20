# Dev environment — build + test in Docker

A reproducible, isolated dev setup: build the engine and run the **full test suite inside
a container**, plus a resource-limited dev orchestrator. Same one-command spirit as the
reference stack, but tuned for *developing on* Ironclad (yours or a fork). Nothing here is
required to merely *use* Ironclad — see [`SETUP.md`](../SETUP.md) for that.

## The build + test gate (the important bit)

A change is only ready to ship once it **builds and the whole suite is green in the
container**. One command does both:

```bash
# from core/
docker compose -f docker-compose.dev.yml run --rm test
```

This builds the dev image (`Dockerfile.dev`) and runs the full suite. **Exit code is the
gate** — non-zero means stop. A JUnit artifact lands in `./dev-artifacts/report.xml` as
your test proof.

No model or network is needed: the tests stub the endpoint, so the gate is deterministic
and fast.

## A dev orchestrator

```bash
# open profile, resource-limited, points at an endpoint you choose (default: localhost)
# GX10_MODEL is optional (default: qwen3.6-35b) — set it only if your served name differs
GX10_BASE_URL=http://host.docker.internal:8000/v1 \
  docker compose -f docker-compose.dev.yml up -d orchestrator-dev
```

- **Endpoint is configurable** (`GX10_BASE_URL`): default points at a model on your host;
  set it to a shared dev/Spark endpoint if you have one.
- **`open` profile** (no auth) — for local development. Use `sealed` for anything exposed.
- **Resource limits** keep it from swamping your machine (tune in the compose file).
- Workdir persists in `./dev-workdir` on the host (engine state under `.ironclad/`, artifacts under `vault/<slug>/`).

Then drive it with the **recommended TypeScript client** from your host (so your code stays on
your machine; build it once per [`SETUP.md`](../SETUP.md)):

```bash
GX10_SERVER_URL=http://localhost:8100 ironclad --codedir /path/to/your/code
# or the zero-dependency Python REPL fallback:
GX10_SERVER_URL=http://localhost:8100 python engine/client.py --codedir /path/to/your/code
```

## Where this fits

This is the **dev** stage. For how a verified change moves toward release, and how the
framework extends itself, see [`self-maintenance.md`](self-maintenance.md). To plug in a
different code-agent CLI, see [`code-agents.md`](code-agents.md).
