"""Deterministic effort & sovereignty resolution (Spec 05 §4).

The registry only ships *defaults*; it never picks a provider (the router does, via P0). But the
*resolution rules* are nailed down here so registry and router can never diverge:

* ``resolve_effort(panel, role, mode)`` — precedence: ``role.effort`` > ``effort_defaults.by_mode[mode]``
  > ``effort_defaults.default``.
* ``effort_to_max_tokens`` — the ONE execution table (Spec 05 §4 / 02 §8). In the reasoning-only path
  the only effective effort lever is ``fanout``'s ``max_tokens``; ``effort_to_template`` covers the
  optional code-CLI lane (a free ``{effort}`` string that knows no ``xhigh`` → maps to ``high``).
  Reserved (#503 MPR-REG-3): both are the Spec-05-§4 resolution tables with full contract tests; the
  current reasoning-only runtime resolves the max_tokens lever inline, so they have no production caller
  yet — kept as the canonical effort→execution contract for the code-CLI lane, not as dead code.
* ``resolve_policy(panel, role)`` — the **single authorized source** of a role's resolved sovereignty
  policy. Router/P0 must read the policy ONLY here, never re-derive it from ``evidence_source`` (no
  second, divergent rule). Carries the registry-local sovereignty defensive: an ``internal`` panel
  never resolves to ``offloadable`` — a contradictory per-role override is a content bug and is
  hard-rejected (``SovereigntyError``), not silently passed through. So the *resolution* is sovereign
  even before P0 enforces the *egress* block (Spec 05 §0/§11).

Note ``use_enum_values=True`` on the models: a validated ``Panel``/``Role`` stores plain strings, not
enum members. Every function coerces through the enum (``Effort(x)`` / ``ProviderPolicy(x)``) so it is
correct whether handed a string or an enum.
"""
from __future__ import annotations

from typing import Optional, Union

from .schema import Effort, EvidenceSource, Mode, Panel, ProviderPolicy, Role


class SovereigntyError(ValueError):
    """Raised when resolution would leak sovereignty — e.g. an internal panel asking to offload."""


# ── Effort → execution mapping (Single-Source; config-overridable via mpr.effort.max_tokens, §8) ──
EFFORT_MAX_TOKENS: dict[str, int] = {
    "low": 2048,
    "medium": 4096,
    "high": 8192,
    "xhigh": 16384,
}

#: The optional code-CLI lane's free ``{effort}`` token (client.py:64 knows no enum / no ``xhigh``).
EFFORT_TEMPLATE: dict[str, str] = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "high",  # template has no xhigh → the larger token budget carries the extra effort
}


def _effort_value(e: Union[Effort, str]) -> str:
    return Effort(e).value  # normalise enum|str → canonical string


def resolve_effort(
    panel: Panel,
    role: Role,
    mode: Union[Mode, str, None] = None,
) -> Effort:
    """Resolve a role's effort: role.effort > effort_defaults.by_mode[mode] > effort_defaults.default."""
    if role.effort is not None:
        return Effort(role.effort)
    defaults = panel.effort_defaults
    if mode is not None:
        # robust across both Mode enum classes (registry + router schema) and plain strings:
        # use .value when present, else the raw string — never str(EnumMember) ("Mode.X").
        mode_val = getattr(mode, "value", None) or str(mode)
        by_mode = defaults.by_mode.get(mode_val)
        if by_mode is not None:
            return Effort(by_mode)
    return Effort(defaults.default)


def effort_to_max_tokens(
    effort: Union[Effort, str],
    *,
    table: Optional[dict[str, int]] = None,
) -> int:
    """Map an effort to fanout max_tokens (the only effective reasoning-only lever). ``table`` lets the
    router pass a config-overridden mapping (mpr.effort.max_tokens); defaults to ``EFFORT_MAX_TOKENS``."""
    tbl = table if table is not None else EFFORT_MAX_TOKENS
    return tbl[_effort_value(effort)]


def effort_to_template(effort: Union[Effort, str]) -> str:
    """Map an effort to the optional code-CLI lane's ``{effort}`` string (xhigh → high)."""
    return EFFORT_TEMPLATE[_effort_value(effort)]


def _policy_from_evidence(evidence_source: Union[EvidenceSource, str]) -> ProviderPolicy:
    src = EvidenceSource(evidence_source)
    # internal stays local; public research (external/mixed) may be offloaded.
    return ProviderPolicy.LOCAL_ONLY if src == EvidenceSource.INTERNAL else ProviderPolicy.OFFLOADABLE


def resolve_policy(panel: Panel, role: Role) -> ProviderPolicy:
    """Resolve a role's sovereignty policy — the SINGLE authorized source.

    Precedence: ``role.provider_policy`` (if set) > derived from ``panel.evidence_source``
    (internal→local-only, external/mixed→offloadable). Defensive invariant: an ``internal`` panel
    never resolves to ``offloadable`` — a role that explicitly asks to offload on an internal panel is
    a content bug and is hard-rejected (``SovereigntyError``). Overriding the *other* way (a role going
    ``local-only`` on a mixed/external panel — e.g. the risk-assessment technical role) is always
    allowed: more restrictive is safe.
    """
    if role.provider_policy is not None:
        candidate = ProviderPolicy(role.provider_policy)
    else:
        candidate = _policy_from_evidence(panel.evidence_source)

    if (
        EvidenceSource(panel.evidence_source) == EvidenceSource.INTERNAL
        and candidate == ProviderPolicy.OFFLOADABLE
    ):
        raise SovereigntyError(
            f"role {getattr(role, 'role', '?')!r} resolves to 'offloadable' on an internal panel "
            f"(domain={panel.domain!r}) — internal evidence must never be offloaded"
        )
    return candidate
