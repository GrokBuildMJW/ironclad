# Web search (`web_search`)

Ironclad gives the model a first-class `web_search` tool for current, time-sensitive, or
post-training-cutoff information — instead of letting it improvise a shell web fetch (which is
blocked and corrupts the terminal). The tool is **read-only, concurrency-safe, capability-gated**,
and its result always ends with a sources reminder.

The design is split into four independent blocks so a backend can be swapped without touching the
rest of the engine.

## 1. Tool definition (`engine/gx10.py`)

`web_search` is an OpenAI-style function tool with a **grammar-clean** schema —
`{query, allowDomains?, blockDomains?}`, no `minLength`/`pattern`/`minItems` (the structured-outputs
path rejects those). The strict rules are enforced in a **validator**, not the schema:

- `query` ≥ 2 non-whitespace characters;
- `allowDomains` and `blockDomains` are **mutually exclusive**;
- domains are normalized (scheme + path stripped, lowercased) and **wildcards (`*`/`?`) are rejected**.

A violation returns a short, model-readable reask string (the Validate→Reask contract) — the call is
re-emitted, never silently swallowed. The validator lives in `engine/websearch.py` and is pure
(no engine import, no network).

## 2. Prompt + sources

The orchestrator prompt steers current-info requests to `web_search` and requires the answer to end
with a **`Sources:`** list of the relevant URLs as Markdown links. This is not left to the prompt
alone: the tool result is built by a deterministic formatter that **always appends a sources
reminder** (after the max-output cap, so it can never be truncated away) and lists the unique result
URLs. "Always cite sources" is therefore a testable invariant.

## 3. Renderer (status footer)

When a search runs, the engine emits a `[search] q="…" n=<batches> ms=<duration>` control frame into
the stream (the same mechanism as `[perf]`/`[agent]`). Every client routes it to the **status
footer** ("web N · Xms") and strips it from the chat — the Ink client and the Python CLI/TUI clients
all filter it. The structured result stays internal to the renderer; the model only ever receives
clean text plus the sources.

## 4. Provider adapter (the seam)

Search executes through a vendor-neutral `WebSearchAdapter` seam (`engine/websearch_adapters.py`)
selected by `search.adapter`, **independent of the provider dispatcher** — so a native-search
deployment with no CLI provider still offers the tool. Every adapter returns a structured
`SearchOutput {query, results: list[SearchBatch | str], durationMs}`.

| `search.adapter` | What it does | Availability |
|---|---|---|
| `cli` (default) | Delegates to a web-capable CLI provider via the existing provider lane (captured output → immune to the console-write break). | a web-capable CLI provider is configured |
| `brave` | **Native** HTTP search (standard-library `urllib`, no extra dependency), applying the domain filters via `site:`/`-site:` operators, normalizing the response into `SearchHit`s with a measured duration. **Local setups only** — under `server` mode it falls back to `cli`. | the API key is present |
| `mock` | Deterministic, network-free — for tests and a zero-config demo. | always |

All vendor-specific literals (the API host, the subscription header) are confined to the native
adapter module; the rest of the engine knows only the vendor-neutral `search.adapter` value. The seam
is stateless and fail-soft: a timeout / HTTP error / decode error becomes a short readable note, never
an exception into the tool loop.

## Trust profile (`sealed`)

Outbound web search is an egress, so it is **blocked under the `sealed` (sovereign/loopback) trust
profile** by default — the tool is not offered, and a direct/manual call returns a deterministic
refusal. `open`/`token` allow it. An operator opts in with `security.web_in_sealed=true` (a boot-only
frozen key — a runtime `/config set` cannot lift the seal). `web_search` is intentionally **not** a
client-local tool, so a thin client cannot bypass the server-side gate.

## Configuration & the key

See [`config-runtime.md`](config-runtime.md) for the full `search.*` block and the `GX10_SEARCH_*`
env overrides. The **API key is never config**: the config holds only the *name* of the env var
(`search.api_key_env`, default `GX10_SEARCH_API_KEY`); the value is read from the environment at boot.
Native (`brave`) search is local-only, so supply the key in the local user environment. If the
configured adapter is unusable (e.g. `brave` with no key), web search stays **off** fail-soft — a
`[search]` boot note explains why, and the server still boots (search is optional).
