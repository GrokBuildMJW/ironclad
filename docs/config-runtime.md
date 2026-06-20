# Runtime config control — `/config get` & `/config set`

ironclad merges its configuration once at startup with the precedence

```
code-defaults  <  file / conf  <  env
```

into a single in-memory tree (`gx10._EFFECTIVE_CFG`). `/config get` and `/config set` let an operator
**read and override** any key of that tree **at runtime**, without restarting the server. (The standalone
launcher also accepts a few CLI flags like `--model`/`--workdir`; the **headless server** — which is what
actually builds `_EFFECTIVE_CFG` — takes its config from defaults + file/conf + env only.)

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

> `/config get` renders a key whose value is `None` (a legitimate default, e.g. `providers.default_id`)
> the same as a truly absent key — both show `(not set)`; it does not distinguish them.

## Semantics

1. `set` writes the (coerced) value into `_EFFECTIVE_CFG` at the dotted path.
2. It then calls `_apply_config(_EFFECTIVE_CFG)` to **re-derive the engine globals** from the full tree
   (idempotent; reads the merged tree as the source of truth).
3. `_apply_config` re-derives **all** core globals from the whole tree and simply **ignores** sections it
   does not model — so setting a **plugin** key normally succeeds quietly (the plugin re-reads its own
   slice of `_EFFECTIVE_CFG` on its next use). The `stored (not a core global: …)` note is a defensive
   fallback shown **only if `_apply_config` raises** (e.g. the live tree is missing a section core expects);
   the dotted write itself still stands.
4. With no live config yet (`_EFFECTIVE_CFG is None`, i.e. before the server has merged its config) `set`
   is a friendly no-op.

## Frozen (boot-only) keys

Some keys wire something at **startup** that a later write cannot re-thread. Currently frozen
(`_FROZEN_CONFIG_KEYS`): **`setup.type`** (selects the offload runner — see [`setup-types.md`](setup-types.md))
and **`security.profile`** (builds the trust policy + the effective bind host, e.g. `sealed`→loopback — see
[`security.md`](security.md)). Mutating either at runtime would be incoherent, so `/config get <key>` still
reads them but `/config set <key> …` is **refused** with a clear message ("boot-only — set it in the
deploy"). The frozen set lives in core, generic and extensible. Change a frozen key in the config file /
env and restart.

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
