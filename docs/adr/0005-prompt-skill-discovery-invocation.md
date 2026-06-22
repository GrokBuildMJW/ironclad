# ADR-0005 — Prompt/skill discovery, per-item invocation & catalogue endpoint

- **Status:** Accepted (implemented) — epic #146 (sub-issues #147 discovery, #148 invocation, #149 catalogue/autocomplete, #150 seed). For what ships see [`status.md`](../status.md). Closes the slash-resolution gap left open by [ADR-0003](0003-prompt-library.md) D5.
- **Date:** 2026-06-22
- **Context sources:** the command router `engine/commands.py` + `engine/gx10.py::_dispatch`, the loaders `_load_skills`/`_discover_*` and the live `_PROMPTS`/`_PLAYBOOKS`/`_PLUGIN_TOOLS` maps, `ack/{prompt,promptgen}.py` (the LLM-free elicitation state machine), `engine/server.py` + `engine/security.py` (`GATED_PATHS`), the TypeScript client `clients/ink/src/{commands.ts,net/server.ts,ui/App.tsx}`, and [ADR-0002](0002-core-always-on-skills.md)/[ADR-0003](0003-prompt-library.md).

## Context

After [ADR-0003](0003-prompt-library.md)/#105, prompts ship as always-on core built-ins but were only reachable through the **model-elected `use_prompt` tool** — there was **no way to list the loaded library and no deterministic per-item invocation**, and slash autocomplete was a static, hand-maintained list. ADR-0003 **D5** specified a router slash-resolution surface, but #110 delivered only the tool. This epic makes the delivered library **usable, discoverable, and seeded**, reading the **one** registry the loaders already populate — no second mechanism.

## Decisions

**D1 — Discovery reads the one loaded registry (`/prompts`, `/skills`).** A single read-only helper `gx10._catalogue_snapshot()` projects the live `_PROMPTS` / `_PLAYBOOKS` / `_PLUGIN_TOOLS` maps (populated by `_load_skills` at startup) into `{prompts:[{name,description,languages}], skills:[{name,kind,description}]}`. `/prompts` lists prompt items; `/skills` lists both skill kinds (`SKILL.md` playbooks **and** typed `CASE`+`run` tools, incl. the MPR built-in). No re-scan, no parallel index. Both are server commands advertised in `/help`. (Rejected: re-running `ack.catalogue.build_catalogue` for discovery — a second filesystem scan that can drift from what is actually loaded.)

**D2 — Per-item invocation closes D5: `/<prompt-name>` resolves in the router.** `_dispatch` resolves a leading token against the loaded prompt catalogue **after every built-in command** (so a real command is never shadowed) and **before** the model-turn fallthrough (an unknown `/x` still becomes a turn). It reuses the existing **LLM-free** `ack.promptgen.run_prompt` state machine: assemble the finished prompt in the target language when all required variables are present, else return the guiding questions for what is missing. Argument parsing peels a **trailing** `--lang xx` only, then routes a **single positional** value to the lone required variable **verbatim** (a `=`/`--lang` inside a code/diff value is preserved), or parses explicit `var=value` tokens. Name resolution is case-insensitive and crash-safe on blank input.

**D3 — `use_prompt` is retained (additive), and there is no generic dispatcher.** The model-elected `use_prompt` tool path is unchanged; `/<prompt-name>` is the additive deterministic surface. There is **no** generic `/prompt run X` / `/skill X` "magic" command — discovery **lists**, items stay `/<name>`. The "offer to save the assembled prompt as a new library item" from ADR-0003 D5 is **deferred** (it belongs to the curation/maintenance follow-up epic, not here).

**D4 — Autocomplete is server-fed via a guarded `GET /catalogue`.** A new read-only endpoint serves the same `_catalogue_snapshot` (one surface). It is **gated** — added to `GATED_PATHS`, so under `token`/`sealed` it 401s without the deployment secret (the registry snapshot is deployment detail, like `/tasks`). The TypeScript client fetches it **lazily** on the first slash-menu open and merges the loaded **prompt** names into autocomplete as directly-invocable `/<name>` entries; a built-in command wins on a name collision. **Skills are not injected** (they are not bare-slash invocable — that would create dead completions); they stay discoverable via `/skills`. Fail-soft: an older server (no `/catalogue`), a gated/closed session, or a network error → built-in commands only, with retry on a later open.

**D5 — Seed selection criteria.** The curated starter set is small (target 5–8), general-purpose, declarative (`kind: prompt`, one MD file + a `locales/de.json` overlay), **multilingual (EN+DE)**, and **must pass `ack.gate`** (required variables used in the template; each declared language assembles cleanly). No blind import of foreign-ecosystem skills. This epic brought the set to **7** (`code-review`, `commit-message`, `bug-report`, `explain-code`, `pr-description`, `refactor-plan`, `test-plan`). "New prompt = drop an MD file" — no engine change.

## Boundary / security

Secret-free; English-only **code** (prompt templates + `locales/*.json` are translatable **data**, the multilingual feature — not a code-language violation). `/catalogue` is behind the same `_guard()` as `/tasks`/`/doctor` and carries only capability names + descriptions + languages/kind (never file paths, sources, or secrets).

## Consequences

- The delivered library is now usable end-to-end: list (`/prompts`/`/skills`), invoke (`/<name>`), autocomplete (catalogue-fed), seeded (7 built-ins) — all on the one loaded registry.
- The command router grows exactly one resolution branch + the elicitation helpers; the server grows one guarded GET; the client grows one fetch + a completion merge. Small, additive, no new infrastructure.
- The intelligent **curation/research/auto-maintain** flow (and "save assembled prompt as an item") is explicitly out of scope — a separate follow-up epic.

## Alternatives considered

- **Catalogue (`ack.catalogue`) as the discovery source** — rejected for discovery: a second scan that can drift from the loaded registry (D1).
- **A generic `/prompt`/`/skill` dispatcher** — rejected: items stay `/<name>` (D3).
- **Injecting skill names into autocomplete** — rejected: skills are not bare-slash invocable, so it would create dead completions (D4).
- **Reusing the model-guided `use_prompt` as the only surface** — rejected: it requires the model to drive elicitation; a deterministic `/<name>` surface is what "items stay `/<name>`" needs (D2).
