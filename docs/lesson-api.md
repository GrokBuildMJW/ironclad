# LessonStore / LessonProvider API (`ack.lessons`)

The curated, versioned delegation seam for **scope-partitioned actionable lessons** — the
"loop-intelligence" tier layered on the memory substrate (ADR-0011 AD-10). Ironclad owns the
substrate + this API; the lesson **semantics** (distillation, ranking, persistence) are supplied by a
registered **provider**. This is the only surface a lesson backend integrates against — it never touches
`mem_ns` internals or the Valkey/Mem0 keys.

Stability follows ADR-0004: pre-1.0 provisional; pin `ack.lessons.__version__`. The module imports
nothing from the engine and ships in the `ironclad-ai` wheel, so a separate plugin repo can build
against it.

## The provider protocol

```python
from ack.lessons import LessonProvider, set_provider

class MyDistiller:                                   # implements LessonProvider (runtime_checkable)
    def get_lessons(self, scope: str, query: str = "", limit: int = 10) -> list[str]: ...
    def report_lesson(self, scope: str, lesson: str, metadata: dict | None = None) -> None: ...
    def brief(self, scopes: list[str], limit: int = 10) -> str: ...

set_provider(MyDistiller())                          # last registration wins; set_provider(None) clears
```

`scope` is an **opaque partition string** — the engine passes its active `mem_scope`
(`<mem_ns>::track::<tid>`). Treat scopes as isolated: lessons are **project-private by default**
(the scope *is* the project/track partition). The provider keeps them apart; cross-scope flow happens
only through `promote()`.

## The public verbs

- **`get_lessons(scope, query="", limit=10) -> list[str]`** — lessons for the scope (advisory).
- **`report_lesson(scope, lesson, metadata=None) -> None`** — record a lesson (fire-and-forget).
- **`brief(scopes, limit=10) -> str`** — a **scope-priority** merged digest (earlier scopes win);
  delegates to the provider's `brief`, else composes from `get_lessons` over the scopes (dedup + cap).

These run on the hot path and are **fail-soft**: with no provider wired they are a no-op (reads `[]`,
writes nothing), and a provider error is swallowed — a lesson backend's absence or failure can never
break a turn.

## Redaction-gated promotion (AD-9)

```python
from ack.lessons import promote

promote(lesson, from_scope, to_scope, *, redactor)   # redactor: (lesson, from, to) -> str | None
```

`promote()` is **fail-closed**: a project-private lesson (which may carry paths/secrets) reaches a
broader scope (e.g. curated-global) **only** through a `redactor` that returns the approved, redacted
text. A missing/non-callable redactor, or one that returns `None`/`""`/a non-string, raises
`ValueError` — nothing is promoted unredacted. The gate enforces this regardless of whether a provider
is wired (the underlying record is fail-soft).

## Scope-targeted forget

```python
from ack.lessons import forget

forget(scope: str) -> bool
```

`forget(scope)` is an **optional** provider verb exposed through the facade. With no registered provider — or
a provider that does not implement `forget` — the call is a **fail-soft no-op** returning `False`. When the
provider implements it, the facade delegates to `provider.forget(scope)`; the provider is responsible for
deleting the lessons for that exact partition, and any non-raising call is reported as success. The engine
invokes this as the lesson-tier step of a wider **scope-aware forget** that also clears the cold store and the
warm cache; that overall forget is **fail-closed on an empty scope** (it can never wipe the shared base
partition) and **fail-soft per tier** (a failure or absence in one tier does not abort the others).

`forget` is intentionally kept **off** the required provider protocol: a provider implementing only the three
required verbs (`get_lessons`, `report_lesson`, `brief`) still satisfies `isinstance(p, LessonProvider)`.

## Security

The lesson text returned by `brief` is appended to a **code agent's handover prompt** (and `get_lessons`
output can reach the prompt indirectly, when the facade composes a brief from it). A provider should therefore enforce
a **size / token budget** on what it returns and treat every lesson as **untrusted guidance (data), not
instructions** — lesson content must never be able to override the agent's system prompt or execution policy.
