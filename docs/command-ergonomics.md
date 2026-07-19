# Command ergonomics

Ironclad's slash-command surface is wide (dispatch, config, lifecycle, projects, ACE, generation, …).
These ergonomics keep it fast to drive without memorizing every flag. They are all **deterministic and
zero-cost** — no model call is involved in resolving, suggesting, or completing a command — and every
layer resolves to the exact canonical command, which is then re-parsed by the one fail-closed dispatcher
(`engine.gx10._dispatch`). The command surface is described in parallel by `engine.command_spec` (a
machine-readable spec kept in lockstep with the dispatcher by a CI parity guard); the terminal client reads
it from `GET /catalogue`.

## Aliases

Short aliases expand to the canonical command before anything runs:

| alias    | expands to        |
|----------|-------------------|
| `/lg`    | `/lifecycle gate` |
| `/cfg`   | `/config`         |
| `/keys`  | `/config keys`    |
| `/cfgget`| `/config get`     |
| `/cfgset`| `/config set`     |
| `/pj`    | `/project`        |
| `/gen`   | `/generate`       |

`/lg --tree X` runs exactly as `/lifecycle gate --tree X`.

## Did-you-mean (typos cost nothing)

A mistyped command that is close to a real one (edit distance ≤ 2) is **not** sent to the server — the
client shows a suggestion instead, so a typo never bills a model turn:

```
/confog rag on
  unknown command — did you mean  /config ?
```

A bare `/<name>` that is **not** close to any command still forwards, so a prompt-library item invoked as
`/<prompt-name>` continues to work.

Unknown `/config set` keys use the same dependency-free edit-distance primitive against the config schema,
but first account for the common shorthand of typing only a key's leaf name. A unique exact leaf suggests
its full dotted key (`language` → `generation.language`); an ambiguous leaf lists at most five sorted
matches and reports how many remain instead of choosing one. If no leaf matches, only a full dotted key
within edit distance 2 is suggested. Inputs with no plausible match keep the original refusal unchanged.

## Unambiguous prefix

A prefix that matches exactly one **non-destructive** command auto-resolves (`/stat` → `/status`). A prefix
of a destructive or costly verb is only *suggested*, never auto-run.

## Argument autocomplete

In the terminal client, once you are past the verb the suggestion menu completes the command's
**subcommands, flag names, and flag choices** from the spec:

```
/lifecycle           → gate
/lifecycle gate --    → --slug  --tree  --ledger  --stages
/lifecycle gate --stages   → tests  reviews  delivery
```

Accepting a suggestion inserts the token into the line (it does not reset it to `/verb`).

## Discovery

- `/config keys` lists every settable dotted config key; boot-only keys (wired once at startup) are
  flagged. `/config set` **refuses an unknown key** instead of silently writing it and offers bounded,
  deterministic near-match guidance when the schema has a plausible candidate.
- `/skills` shows each tool's parameters; `/prompts` lists the prompt library.
- `/help` groups the commands by danger tier (read-only / mutating / destructive / costly).

## Confirm before a destructive command

A destructive operation (currently `/project delete`, including `--purge`) is **not** run on first ask.
The server replies with a confirmation prompt and changes nothing; re-run the command with a trailing
`--yes` to proceed:

```
/project delete demo
  ⚠ project delete: irreversible — this can delete work; re-run with confirmation to proceed
/project delete demo --yes      # now it runs
```

This decision is made **server-side** (the danger tier comes from the spec, never from the model), so it is
uniform across every client. Read-only, mutating, and costly commands are unaffected.

## `/ace` ergonomics

- `/ace warmup` and `/ace eval` **default their `--ledger`** to `<root>/.devloop/ledger.jsonl` — no path to
  type in the common case.
- `/ace eval` reports a plain-language verdict ("ACE learned from N past run(s) using X model call(s) … Y%
  fewer than the evolutionary baseline"); the paper's J-001/J-002 markers are kept as a parenthetical.

## Localization

The engine's own user-facing chrome — including the confirm reason and the `/ace` verdict — is localized
through the message catalog (`engine/messages.py`); English is the source and default, with a German
overlay selected by `generation.language` / `GX10_LANGUAGE`. The public export is English-only; German
lives only in the catalog, never hardcoded in code.
