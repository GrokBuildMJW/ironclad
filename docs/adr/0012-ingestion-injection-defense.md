# ADR-0012: Ingestion prompt-injection defense (layered)

## Status

Accepted — mandatory and fail-closed.

## Context

Files, command output, directory and forge reads, fetched pages, web search, parallel/provider reasoning,
memory retrieval, MPR, and other plugin results can contain content-borne instructions. They enter the model
context as data, but a model can still mistake an embedded role switch or tool call for an instruction.

The existing `_INGESTION_TOOLS` set also controls a destructive head/tail character cap. It cannot represent
the full trust boundary because structured or already-budgeted provider, memory, and plugin payloads must not
be truncated or corrupted.

## Decision

The engine applies one mandatory post-serialization fence before an untrusted tool result enters model
context:

1. `_INGESTION_TOOLS` remains the character-cap classification for file, directory, command, fetch, forge,
   and review reads.
2. `_UNTRUSTED_RESULT_TOOLS` is the distinct trust classification. It adds `web_search`, `parallel_reason`,
   memory reads, and every dynamically discovered plugin result, including MPR.
3. `ack.injection.wrap_untrusted` marks the serialized payload as data and reports precision-first injection
   signals. Structured/already-budgeted results are fenced without the head/tail cap.
4. Import or wrapper failure returns a safe tool error. Raw untrusted bytes are withheld.

`security.injection_defense` and `GX10_INJECTION_DEFENSE` are deprecated tombstones. Their values warn and
are ignored; `/config set` refuses the retired key. There is no disable path, and the generated
[`config-runtime.md`](../config-runtime.md) reference lists it only in tombstone metadata.

## Consequences

- Native and bridged tool results meet the same model-ingestion boundary.
- Fencing adds tokens to every untrusted result; this is the required cost of the trust boundary.
- Heuristic detection still has false negatives. The fence is defense-in-depth, not proof that a model will
  never follow adversarial data.

## Remaining scope

- A higher-recall classifier for obfuscated content.
- Per-source trust levels.
- Output-side exfiltration checks.
- Structured model APIs that preserve data/instruction types end to end.
