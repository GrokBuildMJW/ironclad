# Build your own — describe an idea, let the agents build it

> **You do not have to be a developer.** Ironclad's whole point is that you *describe*
> what you want, and the framework's own agents **plan it, write the code, write a test,
> run the test, and tell you the result**. This guide shows the loop, a worked example,
> and the three things you can build with it. Pre-release — see [`status.md`](status.md).

## The idea in one minute

Ironclad is an **agent that builds things on your machine**:

1. You say what you want, in plain language.
2. The **orchestrator** plans it and breaks it into a task.
3. A **code-agent** (Claude Code, run locally) writes the files and runs the test.
4. You get back the result — working code, a passing test, a short summary.

It runs against any OpenAI-compatible model endpoint (a local one, or a server you
point it at). Nothing about your project leaves your machine — the code-agent works on
your local copy.

## Set it up — let an AI agent do it for you

The lowest-friction path: point an AI coding agent (Claude Code, Cursor, …) at the
**[`AGENTS.md`](../AGENTS.md)** runbook and say *"set up and smoke-test Ironclad."* It is
a step-by-step, self-checking script. Prefer to do it by hand? See
**[`SETUP.md`](../SETUP.md)** (three commands).

You need: Python 3.10+ and a reachable model endpoint. That's it.

## Worked example — watch it build something

Start the orchestrator, **create an initiative** (a named workspace — this is required before any
artefact-producing work; see [`state-and-initiative.md`](state-and-initiative.md)), then ask for something
concrete:

```text
> /project new calc-demo
> Build a function add(a, b) in calc.py that returns a + b,
  plus a pytest test that checks add(2, 3) == 5. Run the test and make sure it's green.
```

What happens (this is a real, verified run — see [`test-report.md`](test-report.md)):

- the orchestrator **plans** the task and hands it to a code-agent;
- the code-agent **writes** `calc.py` and `test_calc.py`, **runs** `pytest`, sees it pass;
- it reports back: *"calc.py and test_calc.py created, test green (1 passed)."*

```python
# calc.py                      # test_calc.py
def add(a, b):                 from calc import add
    return a + b               def test_add():
                                   assert add(2, 3) == 5
```

You described an idea in one sentence; you got working, tested code. That is the loop —
everything below is just *what* you point it at.

## Three things you can build

### 1. A plugin — extend Ironclad without touching its core (Mode A)

Plugins are how you add new **skills, tools, hooks or MCP integrations** through the
stable extension boundary. You keep getting framework updates, because you never fork the
core. Ask the agent: *"scaffold a new skill that does X and register it."* The generator
lays down the skill + a test + its registration; you fill in the idea.

This is the **supported, update-safe** path — best if you want Ironclad to stay Ironclad
and just do more. The full contract (the `CASE` + `run` format, how to enable a plugins
dir) is in **[`plugin-api.md`](plugin-api.md)**.

### 2. Repurpose the whole framework — make it yours (Mode B)

Ironclad is **Apache-2.0**. Take the whole thing and rebuild it for *your* idea — a
different kind of agent, your own tools, your own workflow. In **your** environment you
can change anything. You won't get our updates automatically (you've forked), and that's
fine: it's now your project. Ask the agent: *"change the framework so it does Y instead,"*
and iterate.

This is the **maximum-freedom** path — best if you have your own product in mind.

### 3. Fix a bug — report → reproduce → fix → ship

Found something broken? The same loop fixes it:

1. **Report** it (what you did, what you expected, what happened).
2. **Reproduce** it as a small test (the agent can help: *"write a failing test for this"*).
3. **Fix** it until the test is green.
4. **Ship** it — commit it in your copy (and, if it's a bug in *our* core, open an issue;
   we maintain the core ourselves and turn reports into fixes).

> Note on the core: we keep our published core clean by **not merging outside code into
> it** — but that is only about *our* repository. Your copy is yours to change freely.

## Letting the agent actually change files (permissions)

The code-agent runs headless, so it can't stop to ask you to approve each action. The
**least-privilege posture is the default**: an agent launches with `permission_mode: default`, which
auto-denies commands without blocking — the coder can propose file edits but cannot run commands on your
machine, so it cannot itself run the tests it writes. Nothing runs commands unless you explicitly opt in.

To enable the full *write-and-run-the-test* loop, grant it on the **individual** `agent_id` in your config:
set `permission_mode: bypassPermissions` **and** `capabilities.permission_bypass: true` on that pool entry
(both are required — a `bypassPermissions` mode without the capability is refused fail-closed). That restores
the `--dangerously-skip-permissions` / `bypassPermissions` autonomy for that one agent only, still bounded by
the always-on tooling authorization (only allow-listed executables + command-template shapes) and the OS
command sandbox. Other CLIs use their own approval flags. See [`code-agents.md`](code-agents.md).

`permission_bypass` means the agent can run commands on your machine. That's fine for the intended use —
**your machine, your code, you asked for it** — but it is now opt-in per agent, never a silent default;
understand it before granting it, and keep it to repos you trust.

**Prefer a different coding CLI?** The code-agent isn't locked to Claude Code — it's a
command template (`GX10_AGENT_CMD`), so you can plug in any headless coding agent you
already have. See [`code-agents.md`](code-agents.md).

## Where to go next

- **[`SETUP.md`](../SETUP.md)** — install + run, every step.
- **[`AGENTS.md`](../AGENTS.md)** — have an AI agent set it up for you.
- **[`dev-environment.md`](dev-environment.md)** — develop *on* Ironclad: build + run the
  full test suite in Docker (the gate before you ship a change).
- **[`code-agents.md`](code-agents.md)** — plug in a different coding CLI.
- **[`status.md`](status.md)** / **[`test-report.md`](test-report.md)** — what works, honestly.
- **[`roadmap.md`](roadmap.md)** — where it's going.

**Build something.** Describe the smallest version of your idea and let the agents make it
real — then grow it one sentence at a time.
