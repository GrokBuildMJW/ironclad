# Ironclad terminal client

The recommended interactive client for [Ironclad](../../README.md) — a TypeScript/React
terminal UI on a **purpose-built renderer** (no off-the-shelf TUI framework). It talks to
the orchestrator over plain HTTP only; the Python core is untouched and your code stays on
your machine (the orchestrator's file/command tools are passed through and run locally).

- **Ghost-free resize, smooth token streaming, native-grade scrollback / selection / copy.**
  An own React reconciler + flexbox layout + packed cell buffer with cell-level diffing —
  built to fix the rendering limits of stock terminal stacks.
- **Pinned status bar** (model · memory · throughput · task counts · watcher/autopilot ·
  connection), live-streaming Markdown with **preserved + syntax-highlighted code**, slash-command
  routing **with autocomplete**, native mouse select/copy + **Ctrl+V** paste, and **Esc / Ctrl+C**
  to cancel a running turn.
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

## Commands

Type a message to talk to the orchestrator. Lines starting with `/` are commands — a suggestion
overlay appears as you type (**Tab** completes, **↑/↓** pick, **Esc** dismisses):

- **Local (client):** `/help`, `/reset` (start clean — transcript + server context + summary;
  long-term memory kept), `/resume` (restore the previous session — start is fresh by default),
  `/update [pull]` (rebuild + reinstall the client from source, then restart — needs `GX10_SRC`),
  `/tasks`, `/pending`, `/work`, `/auto on|off`, `/health`, `/exit`.
- **`!<cmd>`** runs a shell command **locally** (PowerShell on Windows) in the codedir, e.g.
  `!git status` — no orchestrator round-trip.
- **Server (forwarded):** `/status`, `/config`, `/clear`, `/context`, `/rag on|off`, `/read`,
  `/cat`, `/ls`, `/write`, `/doctor`, … (the overlay lists them all).

**Sessions are per project.** State is saved in `<codedir>/.ironclad-cli/session.json` (a
self-ignoring directory), so switching projects never overwrites another's session. Start is fresh
by default; `--resume` (or `/resume`) restores it, and on exit the client notes the saved session.

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
| `srcDir` / `GX10_SRC`                  | repo root for `/update` (rebuild from source)      | _(none)_                 |
| `claudeEffort` / `GX10_CLAUDE_EFFORT` | code-agent reasoning effort                        | `high`                   |
| `claudePermissionMode` / `GX10_CLAUDE_PERMISSION_MODE` | code-agent permission mode (default lets the coder run its own tests) | `bypassPermissions`      |

Env-only: `GX10_STATE` overrides the per-project session-state file path; `GX10_RESUME` /
`GX10_NO_RESUME` opt into / force off session resume (`NO_RESUME` wins).

CLI flags (highest precedence): `--server`, `--codedir`, `--max-agents`, and
`--resume` / `--fresh` / `--no-resume` (resume is off by default).

## Known limitations

- **Windows legacy console (`conhost.exe`) — selection/copy & scaling.** The renderer owns scrollback
  and selection, so it enables the alternate screen (`?1049h`) + SGR mouse tracking
  (`?1000`/`?1002`/`?1006`) and the **application**, not the terminal, drives mouse selection. On the
  legacy Windows console a right-click "copy" (or click-drag) is delivered to the app instead of doing
  a native terminal selection, and conhost's copy/reflow can corrupt the layout/scaling. This is a
  terminal limitation, not a renderer bug. *Workaround:* use **Windows Terminal** (or another modern
  terminal) and **hold `Shift` while dragging** to bypass the app's mouse capture for a native
  selection + copy; `Ctrl+V` paste is unaffected. Shares the alt-buffer + mouse-tracking class with
  issue #256 (the client-side teardown fix is tracked there).

## Develop

```bash
npm run dev         # run from source via tsx (no build step)
npm run typecheck   # tsc --noEmit
npm test            # node:test API via tsx --test (renderer + UI + net + tools)
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
