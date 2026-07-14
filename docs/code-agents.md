# Code-agents — bring your own coding CLI

Ironclad's thin client runs a **local code-agent** to carry out each handover: it writes
the handover into your working copy and launches a headless coding CLI against it. That
CLI is **not hard-wired to Claude Code** — it's a command **template**, so you can plug in
any headless coding agent (Claude Code, or another one you already have) with **no code
change**.

## How it works

One env var, `GX10_AGENT_CMD`, is the command template. Placeholders:

| placeholder | becomes |
|-------------|---------|
| `{bin}` | the binary (also settable via `GX10_CLAUDE_BIN`) |
| `{model}` | the model the orchestrator picked for the task |
| `{effort}` | the effort level (`GX10_CLAUDE_EFFORT`, default `high`) |
| `{permission}` | the permission mode (`GX10_CLAUDE_PERMISSION_MODE`, safe default `default`) |
| `{prompt}` | the instruction (stays a **single argument**, even with spaces) |
| `{feedback}` | a result-capture path — a CLI that writes its final message to a file (e.g. Codex `-o {feedback}`) gets a deterministic fallback if it skips the feedback file (point 4); optional |

Use **only the placeholders your CLI needs** — drop the rest. The default template is
Claude Code's shape, so nothing changes unless you override it.

The contract any code-agent must satisfy is simple, and stated in the prompt itself:

1. run **headless / non-interactive** (no prompts to a human),
2. be able to **write files** in the working directory,
3. **read** the handover at `.ironclad/agent/handovers/<ID>_<AGENT>.md`, do the task, and
4. **write** a short result to `.ironclad/agent/feedback/<ID>_<AGENT>-feedback.md`, whose first line is
   `status: done`, `status: blocked`, or `status: clarification_needed` as applicable.

The client claims the task before launch so the task board shows it as **in progress**, then
reads that feedback file back and reports it to the server. Only `status: done` advances the task. For
compatibility with prose-only final-message capture, both local and remote autonomous lanes add that status
only when the coder exits zero and the non-empty feedback has no status token; explicit statuses are preserved.
A failed run is unclaimed for retry;
claim/unclaim transport failures remain fail-soft. Whatever CLI you use, if it honours those
four points, it works.

## The agent registry — many agents, config-driven (#449)

`GX10_AGENT_CMD` configures **one** agent for every handover. To run **several** agents and let
the orchestrator pick one per task (e.g. Opus for security/architecture, Sonnet for docs, Codex for
implementation), declare them in the **code-agent registry** — a config block, no code change:

```jsonc
// config.code_agents — a SEPARATE, always-on surface (not the fan-out providers.pool)
"code_agents": {
  "timeout_s": 1800,
  "pool": [
    { "provider_id": "claude-opus",  "kind": "cli", "agent_id": "OPUS",
      "model": "claude-opus-4-8",  "bin": "claude", "display": "Claude Opus 4.8",
      "cmd_template": "{bin} --model {model} --effort {effort} --permission-mode {permission} --print {prompt}",
      "effort": "xhigh", "permission_mode": "default" },
    { "provider_id": "codex", "kind": "cli", "agent_id": "CODEX", "model": "gpt-5.5", "bin": "codex",
      "cmd_template": "{bin} exec -m {model} -s workspace-write -c 'approval_policy=\"never\"' --skip-git-repo-check -o {feedback} {prompt}" }
  ]
}
```

- **`agent_id`** is the agent's stable identity AND the handover/feedback filename token
  (`<ID>_<AGENT>.md`) — so it must be **letters only** (e.g. `OPUS`, `SONNET`, `CODEX`; not
  `CLAUDE_OPUS`). It's what `stage_handover`/`advance_pipeline` accept in their `agent` field.
- Ironclad ships **OPUS** and **SONNET** as **overridable** defaults — override their model/template,
  drop them, or add your own freely; nothing is hard-coded.
- The **server resolves the full spec** (`bin`/`cmd_template`/`model`/`effort`/`permission` and the
  permission-bypass capability) from the
  registry and ships it to the client, which just renders it — so each agent runs with its own command
  shape. The handover's frontmatter `to:`/`effort:` still override the registry model/effort per task.
- An **unknown agent fails closed**: a handover/transition for an agent that isn't in the registry is
  rejected (never silently run as some default). The handover schema's `agent` choices are generated
  from the live registry, so a newly-added agent is immediately offerable.
- **Onboard a new backend while disabled (`enabled: false`).** Register an agent with `enabled: false` to
  onboard it **inert**: it is excluded from the enabled-only launch surface (never offered in the handover
  schema, never boot-probed, never launchable, never a budget-failover peer — even if its id is listed in a
  `code_agents.classes` set), yet it still must be a complete, well-formed spec and it shows up in `/coders`
  as `(onboarded · disabled)`. This is the recommended way to add a new CLI: register it disabled, run **one
  real job** to learn its exact exhausted/quota signal (refine `code_agents.exhausted` if the generic
  quota/rate-limit patterns don't catch it), then flip `enabled: true` to activate it.
- **Runtime switch (`/coders use <id>`).** By default each handover runs the agent the orchestrator
  chose for that task. To override at runtime — e.g. to react to a budget/quota change — pin one agent
  with `/coders use <id>`; ALL handovers then run on it until you `/coders use auto`. The pin
  (`code_agents.pinned`, set via the guarded `POST /coders`) fails closed on an unknown agent and is
  applied at every execution/reconciliation seam, so the override is global and consistent.
- **Hard launch wall-clock (`code_agents.timeout_s`).** Every local coder gets a live, per-launch timeout
  (default 1800 seconds; range greater than 0 through 7200; env `GX10_CODE_AGENTS_TIMEOUT_S`). The server
  resolves it into each `/pending` item, so `/config set code_agents.timeout_s <seconds>` affects the next
  launch without a restart. On expiry both clients kill the complete coder process tree and report a
  **failed** run, so the claim is released and the task is retried — on the same agent — up to its retry
  budget before escalating. A timeout is a normal failure, not a budget-exhausted one: it is classified
  `task-failed` (not `agent-unavailable`), so it does **not** trip the circuit-breaker or fail over to a
  peer. Ink retains only the last 256 KiB of each stdout/stderr stream, with an explicit truncation marker,
  and writes those bounded tails to the per-task diagnostic log.
- **Budget/quota failover (task-class-scoped).** When an agent's run reports it is out of budget/quota,
  the server classifies the run as `agent-unavailable` (a layered check of a JSON error event → a stderr
  regex → an exit code, with patterns in your `conf/`), trips a process-lifetime circuit-breaker for that
  agent, and the next handover **fails over to the cheapest non-tripped peer that is *capable of the
  task's class*** — so an exhausted agent never silently retries forever, and a **security** or
  **architecture** task never falls to a cheaper-but-weaker agent. The task class is derived
  deterministically from the task's `type` (`security`/`security-audit` → `security`, `architecture`,
  `verification` → `analysis`, everything else → `coding`; the model's own claims are not trusted), and a
  `code_agents.classes` map names which agents may serve each class (default `security: [OPUS]`,
  `architecture: [OPUS]`, `coding: [OPUS, SONNET]`, `analysis: [SONNET]`). The staged
  (orchestrator-chosen) agent stays authoritative and a pin still wins — the class only **scopes the
  failover peers**. An unmapped/unknown class imposes no restriction (fail-open); if every capable agent
  is tripped the chosen one is kept (fail-closed — never an out-of-class agent). The classifier is
  **conservative**: an unknown failure is a task failure, not a failover. `/coders` marks tripped agents;
  `/coders use <id>` clears that agent's breaker (use it once the budget refreshes). The breaker resets on
  restart. An agent's *exact* exhausted signal is calibrated from one real run (the shipped patterns are
  generic).
- **Binary resolution + boot probe.** At startup the server probes each enabled agent (prompt-free) and
  is "code-agent available" iff at least one resolves. Each `bin` resolves via `PATH` first (so a stable
  shim named like your `bin` just works), else the optional **`bin_glob`** — a glob whose **newest** match
  (by mtime) is used, for a CLI installed under a **rotating/hashed launcher path**. Env vars (`%VAR%` /
  `$VAR`) and `~` in `bin_glob` are expanded. Keep the concrete install path in your own `conf/` (it is a
  deployment detail, not part of the core mechanism); see the verified Codex example below for the shape.
- **Model validation (advisory, opt-in).** A code-agent may declare `models_probe` (arguments appended to
  the resolved CLI binary to list advertised models) and optional `models_pattern` (regex for extracting
  model ids for display). At boot the server runs the probe best-effort, warns when the configured `model`
  is not advertised, and caches the result; a later launch of that exact mismatching model is refused with
  a named board-visible error instead of spawning a coder that stalls silently. CLIs without a stable
  models-list subcommand simply omit `models_probe`, which keeps validation off and preserves the launch
  path. `models_probe` is intentionally outside tooling-envelope spawn enforcement: it is a diagnostic,
  prompt-free model-list command on an already authorized/resolved registry entry, not a coder handover
  launch. Failed or empty coder runs are also surfaced on the board as `⚠ ERRORED` or `⚠ UNAVAILABLE` with
  captured stderr in both the local lane and the client `/feedback` lane.
- **Read-only Memory MCP (`mcp_template`, #480).** An MCP-capable CLI can LIVE-query the project memory
  during a handover. Put a `{mcp}` placeholder in the agent's `cmd_template` and an `mcp_template` (the
  per-CLI MCP config — Claude `--mcp-config <json>`, Codex `-c mcp_servers.*`; the `{mcp_cmd}`/`{mcp_script}`
  tokens render to the python invocation of `memory_mcp.py`). The MCP is injected whenever a memory service
  is configured and the agent ships an `mcp_template`, regardless of trust profile — otherwise `{mcp}` is
  empty and the launch is unchanged. The connection is passed via the spawned process's env (secret-free,
  never on the MCP wire), and the read is **read-only**, scoped to the project memory namespace.

**Precedence.** Per field the client resolves: an **explicit client-side `GX10_AGENT_CMD` /
`GX10_CLAUDE_BIN`** (the single-agent BYO override below) **wins**, else the **server-resolved registry
spec**, else the built-in Claude default. So setting `GX10_AGENT_CMD` on the client always takes effect —
even against a default server that ships OPUS/SONNET — while a deployment that configures the registry and
sets no client override gets each agent's own command shape.

## Tooling envelope (mandatory launch allow-list)

`GX10_AGENT_CMD`, `GX10_CLAUDE_BIN`, and each `code_agents.pool[*].cmd_template` decide which local
program a coder handover may spawn. The tooling envelope authorizes that launch surface on every path:

```jsonc
"security": {
  "tooling_envelope": {
    "allow_list": [
      {
        "bin": "claude",
        "cmd_template": "{bin} --model {model} --effort {effort} --permission-mode {permission} --print {prompt}"
      }
    ]
  }
}
```

Every coder-spawn lane authorizes the final executable and command template immediately before spawn. When
`allow_list` is omitted, boot derives exact tuples from enabled CLI entries in `code_agents.pool` and
`providers.pool`; for Claude entries it also derives the engine's canonical stream and non-stream autopilot
shapes. An explicit empty list denies every external spawn. Malformed or empty derived policy also denies.
The retired `security.tooling_envelope.enabled` and `GX10_TOOLING_ENVELOPE_ENABLED` controls warn and are
ignored; they cannot disable authorization. A mismatch is refused fail-closed and no process is started.
The generated [`config-runtime.md`](config-runtime.md) inventory therefore lists the retired key only as
tombstone metadata; there is no live enable row for tooling authorization.

The allow-list is intentionally small and non-secret:

- `bin` names the authorized executable identity. A bare command name may match by basename after normal
  resolution; the candidate's trailing `.exe`, `.cmd`, `.bat`, `.com`, or `.ps1` is ignored for this
  case-insensitive bare-name comparison. A path-shaped value still pins the executable by byte-exact
  realpath identity. `$VAR`/`${VAR}` and a leading bare `~` are expanded; undefined env references remain
  literal. Only `*` and `?` globs are portable.
- `cmd_template` is the authorized template shape, not the rendered prompt. The guard normalizes variable
  fields like model/effort/prompt, but extra flags or a different template refuse.
- `allow_list` is boot-only; `/config set` cannot alter launch authority in a running process.
- The policy applies only to coder invocation surfaces: provider CLI runner, Python/Ink handover clients,
  autopilot launch/reconciler launches, the `review` tool, and `/coders use`. It does not enforce a network
  egress policy. The separate always-on build-boundary tripwire handles approved-design egress posture at
  advance time; its only policy input is `network: none|declared|open` under `## Build policy`.

Keep concrete private paths, wrapper names, and deployment-specific templates in your own config. Public core
docs show only anonymized shapes.

## Model command sandbox (mandatory)

The orchestrator's model-facing `execute_command` tool is separate from the coding-agent launch described
above. Every native or client-bridged model command requires a Linux isolation backend selected by
`security.sandbox` / `GX10_SANDBOX`: `auto` (default), `bwrap`, or `firejail`. `auto` prefers `bwrap`.

No backend means no command: Ironclad returns an actionable fail-closed refusal before starting a subprocess.
Windows therefore refuses model `execute_command` until a supported containment backend exists. Install
`bwrap` on the production Linux host (or `firejail` and select it explicitly). Legacy `off`/`none` values are
ignored with a deprecation warning and cannot restore direct execution.

The Ink `/sh` command is an explicit operator-only shell channel. It does not pass through the model tool and
cannot be invoked by a model tool call. Conversely, the versioned client bridge cannot fall back to `/sh` or
to an older client's unsandboxed `execute_command` implementation.

This command sandbox does not sandbox the separately authorized coding-agent CLI process. Coding-agent
launch authority is governed by the mandatory tooling envelope and by the CLI's own permission/sandbox flags.

## Examples

### Claude Code (default, verified)

Nothing to set — this is the built-in default:

```bash
# equivalent to the default GX10_AGENT_CMD:
export GX10_AGENT_CMD='{bin} --model {model} --effort {effort} --permission-mode {permission} --print {prompt}'
export GX10_CLAUDE_PERMISSION_MODE=default
```

Permission bypass is not an environment-only default. It requires an explicit per-agent policy in the
registry; both the mode and capability must be present:

```json
{
  "agent_id": "OPUS",
  "permission_mode": "bypassPermissions",
  "capabilities": {"permission_bypass": true}
}
```

For the Claude autopilot shape this opt-in renders `--dangerously-skip-permissions`. A bypass mode or
dangerous flag without the matching capability is refused before spawn. The mandatory tooling-envelope
authorization and OS sandbox policy remain independent enforcement layers and are not weakened by this opt-in.

### Codex (OpenAI, verified)

Codex (`codex exec`) plugs in as a provider entry — it **drops** `{effort}`/`{permission}`
(`codex exec` rejects `--effort`/`--permission-mode`/`-a`), takes approval via `-c` and sandbox
via `-s`, and uses `-o {feedback}` so its final message is captured (the hybrid fallback to the
feedback file, point 4):

```bash
export GX10_AGENT_CMD='{bin} exec -m {model} -s workspace-write -c '\''approval_policy="never"'\'' --skip-git-repo-check -o {feedback} {prompt}'
```

`{bin}` resolves to the `codex` launcher (under `%LOCALAPPDATA%\OpenAI\Codex\bin`; the path is
hashed/rotates, so resolve the newest at boot). The four-point contract is unchanged — Codex reads
the handover, works the repo, and writes the feedback file; `-o {feedback}` is the deterministic
fallback if it doesn't.

### Grok (xAI, stdout-capture)

Grok (`grok -p`) plugs in the same way but **drops** `{feedback}` — it has no `-o` flag and prints its
answer to **stdout**, so the result is captured from stdout (the fallback for a CLI that skips the feedback
file). `--yolo` is the auto-exec (write) role; `--cwd .` anchors it at the launch directory:

```bash
export GX10_AGENT_CMD='{bin} -p {prompt} -m {model} --cwd . --yolo'
```

`{bin}` resolves to `grok` on `PATH`; `-m grok-build` selects the full coding agent (vs the faster
`grok-composer-2.5-fast`). The four-point contract is unchanged — Grok reads the handover, works the repo,
and writes the feedback file; stdout is the deterministic fallback if it doesn't.

### Any other headless coding CLI (the general recipe)

```bash
# Minimal: a CLI that takes a single prompt and works the repo non-interactively.
export GX10_AGENT_CMD='your-cli --some-headless-flag {prompt}'
```

### Kimi Code / Grok — starting-point skeletons

> ⚠️ **Verify the flags against your CLI's own `--help`.** The flag names below are
> placeholders for the *shape* — different coding CLIs use different flags (and they
> change). Fill in your CLI's real non-interactive / auto-approve / model flags. The only
> hard requirement is the four-point contract above.

```bash
# Kimi-style (ADAPT the flags to your installed CLI):
export GX10_AGENT_CMD='kimi --model {model} <your-headless-flag> {prompt}'

# Grok-style (ADAPT the flags to your installed CLI):
export GX10_AGENT_CMD='grok <your-headless-flag> <your-auto-approve-flag> {prompt}'
```

If a CLI manages its own model (no `{model}` flag), just leave `{model}` out of the
template — the orchestrator's model hint is then ignored and the CLI uses its own.

## Notes

- **Permissions / autonomy.** The default Claude permission mode is `default`; neither the client nor
  autopilot adds a bypass flag. A deployment that needs unattended command execution must opt in on the
  individual `agent_id` with `permission_mode=bypassPermissions` and
  `capabilities.permission_bypass=true`. Other CLIs have their own approval flags. See
  [`self-maintenance.md`](self-maintenance.md).
- **Concurrency.** `GX10_MAX_AGENTS` bounds how many run at once (default 3).
- **Quick check.** Set `GX10_AGENT_CMD`, start a small handover, and confirm the agent
  writes `.ironclad/agent/feedback/<ID>_<AGENT>-feedback.md`. If it doesn't, the CLI either
  isn't running headless or didn't get a write/approve flag.
