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
| `{permission}` | the permission mode (`GX10_CLAUDE_PERMISSION_MODE`, default `acceptEdits`) |
| `{prompt}` | the instruction (stays a **single argument**, even with spaces) |
| `{feedback}` | a result-capture path — a CLI that writes its final message to a file (e.g. Codex `-o {feedback}`) gets a deterministic fallback if it skips the feedback file (point 4); optional |

Use **only the placeholders your CLI needs** — drop the rest. The default template is
Claude Code's shape, so nothing changes unless you override it.

The contract any code-agent must satisfy is simple, and stated in the prompt itself:

1. run **headless / non-interactive** (no prompts to a human),
2. be able to **write files** in the working directory,
3. **read** the handover at `.ironclad/agent/handovers/<ID>_<AGENT>.md`, do the task, and
4. **write** a short result to `.ironclad/agent/feedback/<ID>_<AGENT>-feedback.md`.

The client reads that feedback file back and reports the task done. Whatever CLI you use,
if it honours those four points, it works.

## The agent registry — many agents, config-driven (#449)

`GX10_AGENT_CMD` configures **one** agent for every handover. To run **several** agents and let
the orchestrator pick one per task (e.g. Opus for security/architecture, Sonnet for docs, Codex for
implementation), declare them in the **code-agent registry** — a config block, no code change:

```jsonc
// config.code_agents — a SEPARATE, always-on surface (not the fan-out providers.pool)
"code_agents": {
  "pool": [
    { "provider_id": "claude-opus",  "kind": "cli", "agent_id": "OPUS",
      "model": "claude-opus-4-8",  "bin": "claude", "display": "Claude Opus 4.8",
      "cmd_template": "{bin} --model {model} --effort {effort} --permission-mode {permission} --print {prompt}",
      "effort": "xhigh", "permission_mode": "acceptEdits" },
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
- The **server resolves the full spec** (`bin`/`cmd_template`/`model`/`effort`/`permission`) from the
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
- **Read-only Memory MCP (`mcp_template`, #480).** An MCP-capable CLI can LIVE-query the project memory
  during a handover. Put a `{mcp}` placeholder in the agent's `cmd_template` and an `mcp_template` (the
  per-CLI MCP config — Claude `--mcp-config <json>`, Codex `-c mcp_servers.*`; the `{mcp_cmd}`/`{mcp_script}`
  tokens render to the python invocation of `memory_mcp.py`). The MCP is injected **only under the `sealed`
  trust profile** and when a memory service is configured — otherwise `{mcp}` is empty and the launch is
  unchanged. The connection is passed via the spawned process's env (secret-free, never on the MCP wire),
  and the read is **read-only**, scoped to the project memory namespace.

**Precedence.** Per field the client resolves: an **explicit client-side `GX10_AGENT_CMD` /
`GX10_CLAUDE_BIN`** (the single-agent BYO override below) **wins**, else the **server-resolved registry
spec**, else the built-in Claude default. So setting `GX10_AGENT_CMD` on the client always takes effect —
even against a default server that ships OPUS/SONNET — while a deployment that configures the registry and
sets no client override gets each agent's own command shape.

## Examples

### Claude Code (default, verified)

Nothing to set — this is the built-in default:

```bash
# equivalent to the default GX10_AGENT_CMD:
export GX10_AGENT_CMD='{bin} --model {model} --effort {effort} --permission-mode {permission} --print {prompt}'
export GX10_CLAUDE_PERMISSION_MODE=bypassPermissions   # so it can also run tests/commands
```

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

- **Permissions / autonomy.** A headless agent can't stop to ask. For the full
  *write-and-run-the-test* loop the agent must be allowed to run commands — for Claude
  Code that's `GX10_CLAUDE_PERMISSION_MODE=bypassPermissions`; other CLIs have their own
  auto-approve flag. Understand it before enabling: it runs commands on your machine,
  against your code. See [`self-maintenance.md`](self-maintenance.md).
- **Concurrency.** `GX10_MAX_AGENTS` bounds how many run at once (default 3).
- **Quick check.** Set `GX10_AGENT_CMD`, start a small handover, and confirm the agent
  writes `.ironclad/agent/feedback/<ID>_<AGENT>-feedback.md`. If it doesn't, the CLI either
  isn't running headless or didn't get a write/approve flag.
