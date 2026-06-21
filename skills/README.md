# Core built-in skills

Built-in skills and prompts that ship with Ironclad and are **always loaded** at startup
(scanned from here by the engine, independent of `GX10_PLUGINS_DIR`). See
[ADR-0002](../docs/adr/0002-core-always-on-skills.md). Third-party/user skills load additively
via `GX10_PLUGINS_DIR` (the open plugin surface, see [`docs/plugin-api.md`](../docs/plugin-api.md)).
