"""Lodestar — opt-in capability/gap → backlog plugin for the Agent-Contract-Kernel.

Lodestar is the **generic mechanism** behind capability-driven backlog work:
track which capabilities a system should have, surface the gaps, derive a backlog,
and drive progress from the deterministic TaskStore. It is a *first-class but
optional* feature (default OFF, gated like autopilot/onboarding) — the framework
ships the mechanism; a vessel supplies the content (its concrete gap-tracking files,
its capabilities). Enable it via ``lodestar.enabled = true``.

This subpackage is the home for everything capability-specific that must NOT sit in
the generic kernel:
  - :mod:`ack.lodestar.spec` — :class:`CapabilityTaskSpec` (makes ``capability``
    mandatory for buildable task types) + the prompt rule that advertises it.
  - (later, phase c) gap-tracking format/parser, backlog generation, and the
    capability doctor checks (gap-mappings, capability-uniqueness).
"""
from __future__ import annotations

from .spec import (  # noqa: F401
    CAPABILITY_REQUIRED_TYPES,
    CapabilityTaskSpec,
    capability_prompt_rule,
)

__all__ = [
    "CapabilityTaskSpec",
    "CAPABILITY_REQUIRED_TYPES",
    "capability_prompt_rule",
]
