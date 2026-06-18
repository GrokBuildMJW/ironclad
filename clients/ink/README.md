# Ironclad terminal client

The recommended interactive client for [Ironclad](../../README.md) — a TypeScript/React
terminal UI on a **purpose-built renderer** (no off-the-shelf TUI framework). It talks to
the orchestrator over plain HTTP only; the Python core is untouched and your code stays on
your machine (the orchestrator's file/command tools are passed through and run locally).

- **Ghost-free resize, smooth token streaming, native-grade scrollback / selection / copy.**
  An own React reconciler + flexbox layout + packed cell buffer with cell-level diffing —
  built to fix the rendering limits of stock terminal stacks.
- **Pinned status bar** (model · throughput · task counts · watcher/autopilot · connection),
  live-streaming Markdown, `/command` routing, find-in-buffer (**Ctrl+F**), native mouse
  select/copy + **Ctrl+V** paste, and **Esc / Ctrl+C** to cancel a running turn.
- Same HTTP + tool-bridge contract as the Python clients, so it drops into an existing
  deployment with no server change.

## Requirements

- **Node.js ≥ 22**
- A running Ironclad **orchestrator** (`engine/server.py`) reachable over HTTP.

## Install

From this directory (`clients/ink/`), install it as a **global command** — like `claude` /
`kimi`, `ironclad` then lives in your npm prefix (not the project), runs from any folder, and
uses the current directory as `codedir`:

```bash
npm install         # dependencies (all permissive licenses — see THIRD_PARTY.md)
npm install -g .    # build + install the global `ironclad` command on your PATH
```

`npm install -g .` builds `dist/cli.js` (via the `prepare` script) and installs the
`ironclad` bin. Prefer not to install globally? Run it from source: `npm run build && node
dist/cli.js`, or `npm run dev` (tsx, no build step).

## Run

```bash
ironclad            # from ANY folder — that folder becomes the working directory (codedir)
```

Override per run with flags: `ironclad --server http://<host>:8100 --codedir <path>`.

> **PowerShell:** wrap `ironclad` in a small function that passes `--codedir (Get-Location).Path`
> (see the top-level [`SETUP.md` → Shell shortcuts](../../SETUP.md)) — PowerShell doesn't always sync
> a child process's working directory to `Set-Location`. bash/zsh need no wrapper.

## Configuration

Settings resolve with the precedence **config file < `GX10_*` env < CLI flags** (later wins).
Secret-free: no private host/IP lives in the package — the config file sits on your machine.

**Config file** (JSON) — `$GX10_CONFIG` if set, else the OS user-config dir:
`%APPDATA%\ironclad\config.json` (Windows) or `~/.config/ironclad/config.json` (macOS/Linux):

```json
{
  "serverUrl": "http://<server-host>:8100",
  "maxAgents": 3
}
```

| Key (file) / env var                  | Meaning                                            | Default                  |
|---------------------------------------|----------------------------------------------------|--------------------------|
| `serverUrl` / `GX10_SERVER_URL`       | orchestrator base URL (the `:8100` port)           | `http://localhost:8100`  |
| `serverToken` / `GX10_SERVER_TOKEN`   | deployment secret (only `token`/`sealed` profile)  | _(none)_                 |
| `maxAgents` / `GX10_MAX_AGENTS`       | max parallel local code-agents                     | `3`                      |
| `agentCmd` / `GX10_AGENT_CMD`         | local code-agent command template (bring your own) | Claude Code template     |
| `claudeBin` / `GX10_CLAUDE_BIN`       | code-agent binary                                  | `claude`                 |
| `tunnelCmd` / `GX10_TUNNEL_CMD`       | client-managed tunnel for the `sealed` profile     | _(none)_                 |

CLI flags (highest precedence): `--server`, `--codedir`, `--max-agents`.

## Develop

```bash
npm run dev         # run from source via tsx (no build step)
npm run typecheck   # tsc --noEmit
npm test            # node:test suite (renderer + UI + net + tools)
npm run licenses    # fail if any non-permissive dependency appears
```

## Provenance — a clean-room, in-house renderer

The terminal renderer (`src/render/`) is **clean-room**: built from public patterns and the
MIT references Glyph and OpenTUI plus stock Ink as study material. No proprietary original
code was read or incorporated, and every module filename is our own. The look-and-feel is an
independent reimplementation of the project's own Python client.

It earns trust by showing its engineering trail: every renderer bug — surfaced through unit
tests, adversarial multi-agent verification, and live use — was fixed **at the root cause**
(never a workaround or a softened test) and locked by a regression test. The suite is green
across the renderer, UI, networking and local tool layers.

## License

Apache-2.0 — see [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE). Bundled third-party components
and their permissive licenses are listed in [`THIRD_PARTY.md`](THIRD_PARTY.md).
