# Runtime config control — `/config get` & `/config set`

ironclad merges its configuration once at startup with the precedence

```
code-defaults  <  file / conf  <  env  <  CLI flags
```

into a single in-memory tree (`gx10._EFFECTIVE_CFG`). `/config get` and `/config set` let an operator
**read and override** any key of that tree **at runtime**, without restarting the server.

These commands are **generic and plugin-agnostic** — core carries no knowledge of any specific section.
Any section (core or plugin) that reads `_EFFECTIVE_CFG` can be steered through them.

## Commands

```
/config                      # the full effective config + its source (read-only summary)
/config get <dotted.key>     # read one key, e.g.  /config get ui.max_lines
/config set <dotted.key> <value>
```

`<dotted.key>` is a dotted path into the config tree; intermediate sections are created as needed.
`<value>` is coerced:

| input | becomes |
|-------|---------|
| `on`, `true`, `yes` | `True` |
| `off`, `false`, `no` | `False` |
| an integer literal | `int` |
| a float literal | `float` |
| anything else | `str` |

Example:

```
/config set ui.max_lines 4000
/config set context.token_budget on
/config get context.token_budget        # → context.token_budget = True
```

## Semantics

1. `set` writes the (coerced) value into `_EFFECTIVE_CFG` at the dotted path.
2. It then calls `_apply_config(_EFFECTIVE_CFG)` to **re-derive the engine globals** from the full tree
   (idempotent; reads the merged tree as the source of truth).
3. If `_apply_config` cannot apply the key (e.g. it belongs to a **plugin** section that core does not
   model), the write still stands and a `stored (not a core global: …)` note is shown — plugin sections
   are expected to re-read their own slice of `_EFFECTIVE_CFG` on their next use.
4. With no live config yet (`_EFFECTIVE_CFG is None`, i.e. before the server has merged its config) `set`
   is a friendly no-op.

## Frozen (boot-only) keys

Some keys wire something at **startup** (e.g. `setup.type` selects the offload runner — see
[`setup-types.md`](setup-types.md)). Mutating them at runtime would be incoherent, so they are **frozen**:
`/config get <key>` still reads them, but `/config set <key> …` is **refused** with a clear message
("boot-only — set it in the deploy"). The frozen set lives in core (`_FROZEN_CONFIG_KEYS`), generic and
extensible. Change a frozen key in the config file / env and restart.

> **When does an override take effect?** Core globals: immediately (step 2). Plugin sections: on their
> next read of `_EFFECTIVE_CFG` (most plugins re-read per request, so effectively the next call).

## Scope & persistence

- The override lives in the **running process only** — it is **not** written back to any file. A restart
  reloads from defaults/file/env/flags. Persist a value by putting it in the config file or an env var.
- Single-process control surface: there is no auth layer here beyond the server's own trust profile
  (see `docs/…` security). Treat it like any other orchestrator command.

## For plugin authors

Expose a runtime toggle by simply **reading your section from `_EFFECTIVE_CFG` per call** and documenting
the keys. No core change is needed — `/config set my_plugin.some_flag on` just works. A robust pattern is
to **decouple a load-gate from a runtime-gate**: an env var decides whether the plugin's tool is
registered at all (off = the engine stays byte-identical), while a `my_plugin.enabled` config key decides
whether the loaded tool is active — toggled live via `/config set my_plugin.enabled on`.
