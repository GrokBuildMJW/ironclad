"""Ironclad Extension SDK — the curated, versioned surface a plugin author builds against.

This module is the **one explicit import surface** for extending Ironclad from a **separate
repository** (`pip install ironclad-ai`). Everything re-exported here is the **public plugin
contract**; everything else under `ack.*` / `engine.*` is internal and may change without notice.

You do **not** need this module to write a basic plugin — a `CASE`+`run` file dropped into a
`skills/` dir and discovered via `GX10_PLUGINS_DIR` (or, packaged, the `ironclad.plugins`
entry-point group, see ADR-0004) needs no imports. Use the SDK when you want to **validate**
your plugin against the same gate Ironclad uses, **derive** the tool schema your `run()` yields,
author the **playbook**/**prompt** skill kinds, or **embed** the registry yourself.

Stability policy (ADR-0004): while Ironclad is pre-1.0 (`0.0.x`) this surface is **provisional**
— additive changes are expected and breaking changes are called out in `CHANGELOG.md`. From
**1.0** on, this module follows **semver** with a one-minor-version deprecation window. Pin the
version you build against; you only ever import from `ack.sdk` for a stable contract.

Example — validate a plugin in your own repo before shipping it::

    from ack.sdk import gate
    assert gate("myplugins/skills/greet.py")   # doctor preflight + schema + sibling test

See `docs/plugin-api.md` for the full contract and the separate-repo workflow.
"""
from __future__ import annotations

# ── Tool kind: CASE + run, schema derivation, programmatic registry ──────────────
from .registry import (  # noqa: F401
    DuplicateRegistrationError,
    Registration,
    RegistrationKind,
    Registry,
    RegistryError,
    derive_tool_schema,
    get_registry,
    task_type,
    tool,
)

# ── Playbook kind: SKILL.md + progressive disclosure ─────────────────────────────
from .playbook import (  # noqa: F401
    Playbook,
    PlaybookError,
    discover_playbooks,
    parse_playbook,
)

# ── Prompt kind: declarative kind: prompt + multilingual assembly ────────────────
from .prompt import (  # noqa: F401
    Prompt,
    PromptError,
    Variable,
    discover_prompts,
    parse_prompt,
)
from .promptgen import (  # noqa: F401
    AssemblyError,
    assemble,
    run_prompt,
)

# ── Registration / eval gate (the same gate Ironclad runs before trusting a skill) ─
from .gate import (  # noqa: F401
    SCAFFOLD_SENTINEL,
    GateResult,
    gate,
    gate_generated,
    gate_playbook,
    gate_prompt,
    gate_tool,
    has_scaffold_sentinel,
    library_items_complete,
    run_sibling_test_hermetic,
)

# ── Shared content i18n (localize templates/labels along a dotted path) ───────────
from .i18n import Localizer  # noqa: F401

# ── Self-hosted catalogue (discover/install/update versioned skills) ──────────────
from .catalogue import (  # noqa: F401
    Catalogue,
    SkillEntry,
    build_catalogue,
    install,
    update,
)

#: The provisional public extension surface (ADR-0004). Membership is the contract; order is not.
__all__ = [
    # tool kind
    "Registry",
    "Registration",
    "RegistrationKind",
    "RegistryError",
    "DuplicateRegistrationError",
    "derive_tool_schema",
    "get_registry",
    "tool",
    "task_type",
    # playbook kind
    "Playbook",
    "PlaybookError",
    "parse_playbook",
    "discover_playbooks",
    # prompt kind
    "Prompt",
    "PromptError",
    "Variable",
    "parse_prompt",
    "discover_prompts",
    "assemble",
    "run_prompt",
    "AssemblyError",
    # gate
    "gate",
    "gate_tool",
    "gate_playbook",
    "gate_prompt",
    "gate_generated",
    "has_scaffold_sentinel",
    "run_sibling_test_hermetic",
    "library_items_complete",
    "SCAFFOLD_SENTINEL",
    "GateResult",
    # i18n
    "Localizer",
    # catalogue
    "Catalogue",
    "SkillEntry",
    "build_catalogue",
    "install",
    "update",
]
