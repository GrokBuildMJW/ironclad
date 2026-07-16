# Plugin API — the stable extension surface

This is the **one open, versioned contract** between Ironclad's core and your code. Build
against *this* and you can extend the agent — add tools/skills — **without forking the
core**, and keep getting framework updates. Everything else (engine internals) is not API
and may change.

## A plugin in 10 lines

A plugin is a Python file in a `skills/` directory with a module-level **`CASE`** dict and
a **`run(...)`** function:

```python
# myplugins/skills/greet.py
CASE = {
    "name": "greet",                 # the tool name the model calls
    "description": "Greet someone by name",
    "capability": "greet",           # unique id for this skill
}

def run(name: str) -> str:           # typed params → a closed JSON schema, auto-derived
    return f"Hello, {name}!"
```

Point Ironclad at the directory and it becomes an agent tool:

```bash
export GX10_PLUGINS_DIR=/path/to/myplugins
python engine/server.py &              # the orchestrator loads plugins at startup
python engine/client.py --codedir .    # then: greet Ada → the model calls greet(name="Ada")
```

That's the whole contract. The core never changed.

> **Built-ins vs 3rd-party.** Ironclad's **built-in** skills/prompts (and MPR) load
> automatically from a fixed core dir at startup — you don't need `GX10_PLUGINS_DIR` for them
> (ADR-0002). This page is the **3rd-party/user** extension surface: your skills load
> *additively* via `GX10_PLUGINS_DIR`, with no core change.

## The contract (what's stable)

- **Discovery.** At startup the engine scans the core built-in dir (always) **and**
  `GX10_PLUGINS_DIR` (or `paths.plugins_dir`, additive for your skills) for `**/skills/*.py`.
  Files starting with `_` are ignored. A broken plugin is skipped, never fatal.
- **`CASE` (a dict).** Required: `capability` (unique). Used: `name` (the tool name;
  defaults to `capability`), `description`. Optional: `domain`, and any metadata you add.
  The **tool `name` must also be unique** across loaded plugins — on a name clash the first
  loaded tool is kept and the rest are skipped with a warning (so a duplicate never silently
  shadows another tool).
- **`run(...)`.** A **synchronous** function. Its **typed signature is the tool schema**:
  each parameter becomes a JSON-Schema property (`str`→string, `int`→integer, …); a
  parameter without a default is *required*. Framework/sentinel parameter names
  (`self`/`cls`/`context`/`identity`/`vessel_id` and a bare `_`) and `*args`/`**kwargs`
  are excluded automatically. Return a string (it goes back to the model as the tool result).
- **Registry registration points** (`ack.registry.Registry`): `register_tool`,
  `register_skill`, `register_task_type`, `discover_skills`, `bind_mcp_provider` — the
  programmatic API behind discovery, if you embed the kernel yourself.

These are the surfaces we keep **semver-stable**. We maintain the core ourselves; your
plugins live outside it.

## Build in a separate repo — the SDK (`ack.sdk`)

You don't need the monorepo. `pip install ironclad-ai` ships the whole contract, and
**`ack.sdk`** is the one curated import surface to build a plugin in its **own repository**:

```python
# your-plugin-repo/ — pyproject pins ironclad-ai>=0.0.12
from ack.sdk import gate, derive_tool_schema   # the SDK surface (ADR-0004)

# validate your plugin against the SAME gate Ironclad runs, before you ship it:
assert gate("myplugins/skills/greet.py")       # doctor preflight + schema + sibling test
```

`ack.sdk.__all__` **is** the public API — the tool kind (`Registry`, `derive_tool_schema`,
`tool`, `task_type`), the **playbook** kind (`Playbook`, `parse_playbook`, `discover_playbooks`),
the **prompt** kind (`Prompt`, `parse_prompt`, `assemble`, `run_prompt`), the registration/eval
**gate** (`gate`/`gate_tool`/`gate_playbook`/`gate_prompt`), shared **i18n** (`Localizer`), and the
self-hosted **catalogue** (`build_catalogue`, `install`, `update`). Everything else under `ack.*` /
`engine.*` is internal and may change. **Pin the version** and import only from `ack.sdk`.

**Stability:** while Ironclad is `0.0.x` this surface is **provisional** (additive change expected,
breaks noted in `CHANGELOG.md`); from **1.0** it follows **semver** with a one-minor deprecation
window. See [ADR-0004](adr/0004-extension-sdk.md).

**Loading a packaged plugin.** A dir of skills loads via `GX10_PLUGINS_DIR` (above). A *packaged*
plugin (installed into the deployment) is discovered through the `ironclad.plugins` **entry-point
group** — no path config, no core change. Advertise the group in your plugin's `pyproject.toml`,
pointing at your package (or a callable / dir path):

```toml
# your-plugin-repo/pyproject.toml
[project.entry-points."ironclad.plugins"]
myplugin = "myplugin"     # a package containing a skills/ dir (or a callable returning a dir)
```

Install it into the same environment as Ironclad and the engine discovers it at startup, additively
alongside built-ins and `GX10_PLUGINS_DIR` — the engine **never imports your plugin by name**
(dependency inversion; the only coupling is the group string). See [ADR-0004](adr/0004-extension-sdk.md).

## Scaffold one (don't hand-write it)

The generator lays down a complete, correctly-shaped case/domain skeleton from a template —
a skill stub, its `CASE` spec, a backlog + gap-tracking doc, a test, and the registration:

```bash
python -m ack.generator --domain <domain-key> --case <case-key> --description "what it does"
python -m ack.generator --help      # all options (--phase, --tier, --dry-run, …)
```

`--domain`, `--case` and `--description` are required; everything else has sensible defaults.

## Notes

- **Sync only for the engine tool path.** An `async def run` still **loads and registers**,
  but it is rejected **at call time** with a clear `ERROR` tool result (the coroutine is closed,
  never awaited) — keep `run` synchronous (do any I/O inside it). The full async registry is
  available if you embed the kernel directly.
- **MCP tools** integrate via `bind_mcp_provider` (the kernel binds an external MCP
  provider rather than re-implementing it).
- **Beyond this typed-tool contract** (available today), a second **playbook** skill kind
  (`SKILL.md` + progressive disclosure), a spec→skill **generator**, and a self-hosted
  **catalogue** (semver + provenance) are designed in [`skill-packaging.md`](skill-packaging.md) /
  [ADR-0001](adr/0001-skill-engine-and-library.md) and under active development.
- **Two ways to use this** (see [`self-maintenance.md`](self-maintenance.md)): extend with
  plugins and keep our updates (Mode A), or fork and change anything in your own copy
  (Mode B) — it's Apache-2.0.
