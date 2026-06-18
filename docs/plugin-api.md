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

## The contract (what's stable)

- **Discovery.** At startup the engine scans `GX10_PLUGINS_DIR` (or `paths.plugins_dir`)
  for `**/skills/*.py`. Files starting with `_` are ignored. A broken plugin is skipped,
  never fatal.
- **`CASE` (a dict).** Required: `capability` (unique). Used: `name` (the tool name;
  defaults to `capability`), `description`. Optional: `domain`, and any metadata you add.
- **`run(...)`.** A **synchronous** function. Its **typed signature is the tool schema**:
  each parameter becomes a JSON-Schema property (`str`→string, `int`→integer, …); a
  parameter without a default is *required*. Framework params (`self`/`context`/
  `identity`/`vessel_id`) and `*args`/`**kwargs` are excluded automatically. Return a
  string (it goes back to the model as the tool result).
- **Registry registration points** (`ack.registry.Registry`): `register_tool`,
  `register_skill`, `register_task_type`, `discover_skills`, `bind_mcp_provider` — the
  programmatic API behind discovery, if you embed the kernel yourself.

These are the surfaces we keep **semver-stable**. We maintain the core ourselves; your
plugins live outside it.

## Scaffold one (don't hand-write it)

The generator lays down a complete, correctly-shaped plugin (skill + spec + test +
registration) from a template:

```bash
python ack/generator.py --help     # scaffold a new case/skill
```

## Notes

- **Sync only for the engine tool path.** An `async def run` is rejected with a clear
  error — keep `run` synchronous (do any I/O inside it). The full async registry is
  available if you embed the kernel directly.
- **MCP tools** integrate via `bind_mcp_provider` (the kernel binds an external MCP
  provider rather than re-implementing it).
- **Two ways to use this** (see [`self-maintenance.md`](self-maintenance.md)): extend with
  plugins and keep our updates (Mode A), or fork and change anything in your own copy
  (Mode B) — it's Apache-2.0.
