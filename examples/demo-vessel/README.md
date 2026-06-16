# Demo-Vessel (öffentlich, runnable)

A minimal, self-contained **vessel** (a use-case docking on Ironclad) that exercises
the Lodestar capability/gap plugin end-to-end — no real data, no secrets. Use it to
see the kernel work in ~30 seconds.

## What's here

```
demo-vessel/
  lodestar.json                               # enables the Lodestar plugin (lodestar.enabled=true)
  tasks/{pending,in_progress,done}/           # the TaskStore (one done task: DEMO-1)
  vault/Research/Todo-Api/
    todo-api-gap-tracking.md                  # the MAPPING (SSOT) + generated status tables
    todo-api-backlog.md                       # GENERATED backlog (do not hand-edit)
```

The `todo-api` domain declares three capabilities: `todo-list` (done — there is a
done task carrying `capability: "todo-list"`), `todo-create` (open), and `todo-auth`
(open, `depends_on: ["todo-create"]` → blocked until create lands).

## Run it

From the repository root (so `ack` is importable, here via `PYTHONPATH=core`):

```bash
# 1) Regenerate the status tables (in the gap-tracking file) + the backlog from
#    the MAPPING + the TaskStore:
PYTHONPATH=core python -m ack.lodestar.tracking --root core/examples/demo-vessel
#    -> todo-api: 3 features -> 1 implemented, 2 not-started . todo-api-backlog.md

# 2) Preflight the workspace (generic checks + the Lodestar capability checks):
PYTHONPATH=core python -m ack.doctor --root core/examples/demo-vessel --lodestar
#    -> RESULT: All checks passed. (one warning: no .mcp.json -- fail-soft)
```

Then open `todo-api-backlog.md`: `todo-create` is the top open entry; `todo-auth`
sits under **Blocked** ("waiting on: todo-create") until a done task implements
`todo-create`. Add a done task with `"capability": "todo-create"` and re-run step 1
to watch the backlog re-rank deterministically.

> Real vessels (e.g. a product or business case) live in the **private** monorepo
> under `vessels/`; only this demo ships publicly.
