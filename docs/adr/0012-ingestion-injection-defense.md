# ADR-0012: Ingestion prompt-injection defense (layered)

## Status
Accepted — foundational increment (epic #1065 / #1068). **Default-off.**

## Context
Ingested content — files (`read_file`), search/web results (`search_files` / `fetch_url` / `web_search`),
directory listings, and tool output (`execute_command`) — is fed to the model with **no trust boundary**. An
autonomous agent reading UNTRUSTED content (a repo file, a fetched page, a command's stderr) can be STEERED
by an embedded instruction-override / role-switch / tool-call injection. The no-guessing, sealed-profile, and
tool-gating layers do not address content-borne steering.

## Decision
A **layered** defense, core-owned + default-off:
1. **Detection** (`ack.injection.scan`) — a precision-first heuristic scan for injection patterns:
   instruction-override, role-switch, role-marker/tag injection, tool-call injection.
2. **Trust boundary** (`ack.injection.wrap_untrusted`) — fence every ingested result as *data, not
   instructions* at the ONE ingestion choke point (#1046 `_cap_ingested_result`), with an explicit warning
   when injection signals are present. Gated on `security.injection_defense` (default off).

Defense-in-depth, composing with the sealed trust profile, tool gating, the `fetch_url` SSRF guard (#1074),
and the tamper-evident audit log (#1067).

## Consequences
- **Default-off (byte-identical):** fencing changes the content format and adds tokens, so it is opt-in.
- Heuristic detection has a false-negative tail (novel phrasings) and can't parse obfuscation — it raises the
  bar, it does not close the door.

## Remaining scope (explicit — NOT faked here)
- An LLM classifier on ingested content (higher recall than heuristics).
- Per-source trust levels (a local repo file vs a fetched web page).
- Output-side exfiltration checks (detect an injected instruction that already influenced a tool call).
- Structured tool-result channels the model can't confuse with instructions.
