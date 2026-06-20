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

Use **only the placeholders your CLI needs** — drop the rest. The default template is
Claude Code's shape, so nothing changes unless you override it.

The contract any code-agent must satisfy is simple, and stated in the prompt itself:

1. run **headless / non-interactive** (no prompts to a human),
2. be able to **write files** in the working directory,
3. **read** the handover at `.ironclad/agent/handovers/<ID>_<AGENT>.md`, do the task, and
4. **write** a short result to `.ironclad/agent/feedback/<ID>_<AGENT>-feedback.md`.

The client reads that feedback file back and reports the task done. Whatever CLI you use,
if it honours those four points, it works.

## Examples

### Claude Code (default, verified)

Nothing to set — this is the built-in default:

```bash
# equivalent to the default GX10_AGENT_CMD:
export GX10_AGENT_CMD='{bin} --model {model} --effort {effort} --permission-mode {permission} --print {prompt}'
export GX10_CLAUDE_PERMISSION_MODE=bypassPermissions   # so it can also run tests/commands
```

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
