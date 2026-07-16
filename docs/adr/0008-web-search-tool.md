# ADR-0008 — Web search as a vendor-neutral adapter seam, trust-gated

- **Status:** Accepted. For what ships see [`web-search.md`](../web-search.md) + [`status.md`](../status.md).
- **Date:** 2026-06-26
- **Context sources:** the clean-room web-search spec (4 building blocks: tool-def, prompt, renderer, provider-adapter), a code-grounded integration audit of the existing engine, and an adversarial review that surfaced four operator decisions.

## Context

A first-class `web_search` already shipped, but minimally: `{query}`-only, an opaque text blob,
runnable **only** by delegating to a web-capable CLI provider (silently unavailable when none is
configured), no domain filters, no structured output, and — a verified gap — **no trust gate** (a
`sealed` deployment egressed web search ungated). The goal was a *proper* capability: strict input,
domain filters, structured results, mandatory sources, a progress indicator, and a trust gate — while
keeping `core/` standalone, secret-free, vendor-neutral and English-only.

The review left four decisions to the operator; they were settled as below.

## Decisions

**D1 — A vendor-neutral `WebSearchAdapter` seam, standalone from the dispatcher.** Search executes
through an adapter selected by `search.adapter`, built at boot independent of the provider registry,
so a native-search deployment with no CLI provider still offers the tool (the registry-bolted gate
would dead-gate it). The capability check is adapter-aware (`cli` → a web provider, `brave` → key
present, `mock` → always) and gates the offer, the steer, the shell-hint and the exec handler in
lockstep.

**D2 — Strict input in a pure validator, not the model-facing schema.** The structured-outputs path
(XGrammar) rejects `minLength`/`pattern`/`minItems`, so the tool schema stays grammar-clean and the
rules (query ≥ 2, allow XOR block, domain normalization, wildcard reject) live in
`engine/websearch.py` as a Validate→Reask validator. The wildcard reject is scoped to the
domain filters — Ironclad is single-tenant (no per-tool permission/ACL layer), so that is the only
meaningful wildcard surface.

**D3 — Native HTTP on the standard library, no new dependency.** The native (`brave`) adapter uses
`urllib.request` (the established `core/` precedent), not httpx/requests, so the standalone wheel
stays pydantic-only. All vendor literals are confined to that one module.

**D4 — Native search is local-only.** Under `server` mode `web_search` falls back to the
CLI-delegate; the native HTTP adapter runs only on a local setup. This avoids new outbound egress
from a sovereign deployment and keeps the secret on the host that actually searches.

**D5 — Sources are deterministic and handler-appended; the model gets text, not JSON.** The structured
`SearchOutput {query, results, durationMs}` stays internal to the renderer; the model receives clean
text plus a `Sources:` block and an always-present reminder (appended after the max-output cap), so
"always cite sources" is a testable invariant rather than a skippable prompt hint.

**D6 — `sealed` blocks outbound search; the gate sits at offer AND exec.** Under the `sealed` trust
profile web search is off by default (operator opt-in via `security.web_in_sealed`, a boot-only frozen
key). The exec re-gate is mandatory because a manual `/tool web_search` call or a hallucinated call
bypasses the offer-gate. `web_search` stays out of `LOCAL_TOOL_NAMES` so a thin client cannot bypass
the server-side gate.

**D7 — Progress reaches the renderer via a text-stream sentinel.** The engine emits a `[search]
q="…" n=<batches> ms=<duration>` frame (the `[perf]`/`[agent]` pattern); every client routes it to the
status footer ("web N · Xms") and strips it from the chat — no wire-protocol change. The synchronous
backends have no separate query-start event, so this single post-completion frame carries the query
and the result count (progress is optional in the spec).

## Consequences

- The secret (`GX10_SEARCH_API_KEY`) is name-indirected and resolved from the environment at boot;
  `core/` ships the vendor-neutral mechanism + a deterministic mock, never a key or a backend lock-in.
- The full surface is documented in [`web-search.md`](../web-search.md) and
  [`config-runtime.md`](../config-runtime.md); the config knobs that wire something at boot
  (`search.enabled`/`adapter`/`api_key_env`, `security.web_in_sealed`) are frozen against runtime
  `/config set`.
- The native path is verified offline against a mock HTTP layer; a live run requires a real key on a
  local setup.
