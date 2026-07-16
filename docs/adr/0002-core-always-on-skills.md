# ADR-0002 — Skill / prompt / MPR engine as core, always-on

- **Status:** Accepted (design) — implementation tracked separately. Records the design; not a claim it ships yet (see [`status.md`](../status.md)).
- **Date:** 2026-06-21
- **Context sources:** [ADR-0001](0001-skill-engine-and-library.md) (the skill engine), `skills/mpr/` (the flag-gated plugin being de-plugined), `engine/server.py`+`gx10.py` (loading), `engine/messages.py` + `skills/mpr/i18n.py` (the two i18n layers), an internal research catalogue.

## Context

The skill engine shipped (v0.0.9) with its algorithms **already in core** (`ack.skillgen` / `ack.catalogue` / `ack.gate` / `ack.playbook` / `ack.registry`). But the **activation model** is plugin-shaped: discovery is gated on `GX10_PLUGINS_DIR`, MPR is a **flag-gated plugin** (`skills/mpr`, `GX10_MPR` off ⇒ no `CASE`/`run`, "byte-identical off"), and there is no always-on core home for built-in skills/prompts. Operator decision: the **skill engine, the prompt library/generator, and MPR** must be **core, always-on** capabilities (invoked via tools); MPR should **consume** the core engine, not carry its own infra. The open plugin surface stays for **3rd-party/user** skills only.

## Decisions

**D1 — Always-on core built-ins.** A fixed core directory (**`skills/`**) is scanned at **startup, unconditionally** (`server.py` loads built-ins regardless of `GX10_PLUGINS_DIR`), via the existing `Registry.discover_skills` / `discover_playbooks` (reused unchanged — only the *roots scanned* change). Built-ins are always available with no configuration.

**D2 — Plugin surface = 3rd-party only.** `_load_plugins` / `_load_playbooks` + `GX10_PLUGINS_DIR` remain as the **additive** extension surface for user/external skills. Built-ins are no longer routed through `plugins_dir`. The "open plugin API" stays — it is just no longer how *built-ins* load.

**D3 — MPR de-plugined to core, always-on.** `skills/mpr` → **`skills/mpr`** (a core built-in, always loaded). The plugin entry shim (`mpr_research.py`) and the **`GX10_MPR` boot flag** are removed; MPR is gated only by a **runtime config `mpr.enabled` (default ON)** — off at runtime hides the tool (a config behavior, not a boot/plugin gate). MPR **consumes** the core registry, `ack.i18n` (D4), `ack.catalogue`, `ack.gate`. Behavior is unchanged when enabled; the 382 MPR tests are the hard regression guard.

**D4 — Shared core i18n (`ack.i18n`).** The MPR-local file-overlay loader (`skills/mpr/i18n.py`) is promoted to core `ack.i18n` — always importable, **MPR-flag-independent**, with a **parameterized locales dir** (each caller points at its own `locales/`). `engine/messages.py` stays the **distinct engine-chrome layer**; no third i18n.

## Migration inventory (alt→new — no parallel / no silent break)

| Existing | Strategy |
|---|---|
| `skills/mpr` (plugin, `GX10_MPR` boot-gated) | **MIGRATE → `skills/mpr`** (always-on built-in; flag → runtime `mpr.enabled` default on; consumes core). 382 tests green. |
| `skills/mpr/skills/mpr_research.py` (plugin entry shim) | **DEPRECATE/remove** — replaced by core registration. |
| `skills/mpr/i18n.py` + `locales/` | **MIGRATE → `ack.i18n`**. |
| `_load_plugins`/`_load_playbooks` + `GX10_PLUGINS_DIR` | **KEEP (3rd-party only)** + add always-on built-in loading. |
| the private export pipeline (bundles `skills/mpr` as flagship plugin) | **UPDATE** — ship MPR from `skills/` as a core built-in. |
| install/deploy scripts (copy `skills/mpr`) + docs (plugin-api/status/README, `GX10_MPR`) | **UPDATE** + `GX10_MPR` **deprecation note** + migration path `GX10_MPR → mpr.enabled`. |
| `engine/messages.py` (engine chrome i18n) | **ADOPT** — distinct layer, documented; no third i18n. |
| the `ack.*` engine modules | **REUSE** unchanged. |

Downstream: the prompt-library work builds on this core base (its prompts are core built-ins too).

## Boundary / security

Secret-free; **English-only code/docs**. Built-ins ship in the core export. Locales/prompt content remain translatable data (not a code-language violation). ACK contracts.

## Consequences

- Built-ins (skills, prompts, MPR) work out of the box, no `GX10_PLUGINS_DIR` needed; extensibility for 3rd-party is unchanged.
- MPR is simpler: no plugin shim, no boot flag, shares core i18n; gated by a normal runtime config.
- **Behavior change:** `GX10_MPR` is removed → deprecation note + the `mpr.enabled` migration path (not a silent break); the old "byte-identical when GX10_MPR off" becomes "tool hidden when `mpr.enabled=off`".

## Alternatives considered

- **Keep the plugin/flag model** (status quo) — rejected by the operator; built-ins should be first-class core.
- **Drop the plugin surface entirely** — rejected; 3rd-party extensibility is kept.
- **Remove the MPR toggle entirely (no off)** — rejected in favor of a runtime `mpr.enabled` (default on) so it stays switchable without a rebuild.
