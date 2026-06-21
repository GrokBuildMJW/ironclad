# Example Ironclad plugin (separate-repo template)

A minimal, **standalone** plugin that builds against the published framework — the shape your own
plugin repo takes. It does **not** live inside Ironclad's core; it depends on `ironclad-ai` and is
discovered through the **`ironclad.plugins` entry-point group** (ADR-0004, `docs/plugin-api.md`).

## Layout

```
example-plugin/
  pyproject.toml                     # pins ironclad-ai; advertises the ironclad.plugins entry point
  ironclad_example_plugin/
    __init__.py
    skills/
      reverse.py                     # a tool skill: CASE + run
```

## Build + install + run

```bash
pip install ironclad-ai             # the framework (provides ack.sdk)
pip install .                       # this plugin — registers its ironclad.plugins entry point

# Ironclad discovers it at startup (no path config), additively with built-ins:
python engine/server.py &           # the orchestrator loads packaged plugins via entry points
# the model can now call: reverse(text="hello") -> "olleh"
```

For local development without packaging, point `GX10_PLUGINS_DIR` at a directory containing a
`skills/` folder instead — both paths use the same `CASE`+`run` contract.

## Validate before you ship

```python
from ack.sdk import gate, derive_tool_schema
from ironclad_example_plugin.skills import reverse
print(derive_tool_schema(reverse.run))    # the auto-derived tool schema
# gate("ironclad_example_plugin/skills/reverse.py")  # add a sibling tests/ file to pass the tool gate
```
