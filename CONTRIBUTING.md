# Contributing to Ironclad

Thanks for your interest — Ironclad is early and openly developed, and feedback from
people running it on real models is exactly what makes it better.

## Issues & discussions first

The most valuable contributions right now are **issues**: a model that needs a tweak
to work, a rough edge in the server/client split, a missing config knob, a bug. Open
an issue (or a discussion for open-ended ideas) — that directly shapes the roadmap.

## How code changes flow (important)

The published tree here is **generated from an upstream source repo** and re-exported,
so direct edits to this repo would be overwritten on the next sync. That's deliberate
(it keeps the public surface and the internals from drifting). In practice:

- **Open a PR or an issue with your change** as usual. A maintainer ports accepted
  changes upstream and they come back on the next publish — **you'll be credited**.
- Keep changes focused and explain the *why*; small, well-scoped PRs land fastest.

## Ground rules

- **Secret-free.** The public surface must never contain private hosts, IPs, keys or
  deployment-specific names. (CI enforces a boundary check upstream.)
- **English only** in everything that's user-facing, model-facing or rendered.
- **Tests stay green.** `pip install -e ".[engine]" pytest && python -m pytest ack/tests -q`.
- **Match the surrounding style** — read the file you're editing first.

## Local setup

```bash
git clone https://github.com/GrokBuildMJW/ironclad.git
cd ironclad
python -m venv .venv && . .venv/bin/activate
pip install -e ".[engine]" pytest
python -m pytest ack/tests -q
```

See [`docs/status.md`](docs/status.md) for the honest, per-component wiring status
before relying on any part, and [`AGENTS.md`](AGENTS.md) if you want an AI agent to do
the setup.

By contributing you agree your contributions are licensed under Apache-2.0.
