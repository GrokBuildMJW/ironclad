"""Adaptive panel generation (skills/mpr/registry/adaptive.py) — Spec 05 §7.5 / §10.

Deterministic, no network: a mocked async ChatTransport returns a tool-call dict, so generate_adhoc_panel
runs through the real ack.emit_validated loop. Covers: a valid adhoc panel (>=MIN_ROLES, distinct,
domain forced to 'adhoc'), no generic fallback (different queries → different role sets), fail-soft to
the nearest skeleton on emit-failure (clone panel) AND on transport-throw with the budget respected,
the raise when there is no skeleton, and nearest_panel selection.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from mpr.registry.adaptive import (
    AdhocGenerationError,
    generate_adhoc_panel,
    nearest_panel,
)
from mpr.registry.guards import check_distinctness
from mpr.registry.loader import PanelRegistry
from mpr.registry.schema import MIN_ROLES, Panel

_MPR_ROOT = Path(__file__).resolve().parents[1]


def _registry() -> PanelRegistry:
    reg = PanelRegistry()
    reg.discover(_MPR_ROOT)
    return reg


def _resp(panel_dict: dict) -> dict:
    return {"choices": [{"message": {"tool_calls": [
        {"function": {"name": "emit_panel", "arguments": json.dumps(panel_dict)}}
    ]}}]}


class FakeChat:
    """Async transport that replays fixed panel payloads (last one repeats)."""

    def __init__(self, *payloads: dict):
        self.calls = 0
        self.payloads = list(payloads)

    async def __call__(self, *, messages, model, temperature, extra_body):
        i = min(self.calls, len(self.payloads) - 1)
        self.calls += 1
        return _resp(self.payloads[i])


class ThrowingChat:
    def __init__(self):
        self.calls = 0

    async def __call__(self, *, messages, model, temperature, extra_body):
        self.calls += 1
        raise RuntimeError("transport down")


def _adhoc(roles, domain="adhoc", mode="evidence-research", evidence="mixed", template="evidence-report"):
    return {"domain": domain, "mode": mode, "evidence_source": evidence,
            "synthesis_template": template, "effort_defaults": {"default": "medium"}, "roles": roles}


_DISTINCT = [
    {"role": "Tech", "lens_prompt": "Bewerte technische Machbarkeit und Architektur sorgfältig"},
    {"role": "Recht", "lens_prompt": "Prüfe regulatorische und rechtliche Vorgaben im Detail"},
    {"role": "Markt", "lens_prompt": "Analysiere Marktchancen und das Wettbewerbsumfeld"},
]
_CLONES = [
    {"role": "A", "lens_prompt": "Bewerte die wirtschaftlichen Folgen genau und sehr sorgfältig"},
    {"role": "B", "lens_prompt": "Bewerte die wirtschaftlichen Folgen genau und sehr sorgfältig"},
    {"role": "C", "lens_prompt": "Etwas ganz anderes über Technik Architektur und Betrieb"},
]


# ── valid adhoc panel ────────────────────────────────────────────────────────────────────────────
def test_adhoc_panel_is_valid_distinct_and_min_roles():
    chat = FakeChat(_adhoc(_DISTINCT))
    panel = asyncio.run(generate_adhoc_panel("Soll ich X einführen?", chat=chat))
    assert isinstance(panel, Panel)
    assert panel.domain == "adhoc"
    assert len(panel.roles) >= MIN_ROLES
    assert check_distinctness(panel) == []
    assert chat.calls == 1  # one emit, no reask needed


def test_adhoc_forces_domain_to_adhoc():
    chat = FakeChat(_adhoc(_DISTINCT, domain="something-else"))
    panel = asyncio.run(generate_adhoc_panel("frage", chat=chat))
    assert panel.domain == "adhoc"  # defining property forced regardless of model output


def test_adhoc_no_generic_fallback_distinct_queries_differ():
    rolesB = [
        {"role": "Sicherheit", "lens_prompt": "Sicherheitslücken und Angriffsflächen prüfen"},
        {"role": "Kosten", "lens_prompt": "Kostenstruktur und TCO bewerten"},
        {"role": "UX", "lens_prompt": "Nutzererlebnis und Akzeptanz beurteilen"},
    ]
    pA = asyncio.run(generate_adhoc_panel("Frage A", chat=FakeChat(_adhoc(_DISTINCT))))
    pB = asyncio.run(generate_adhoc_panel("Frage B", chat=FakeChat(_adhoc(rolesB))))
    labelsA = {r.role for r in pA.roles}
    labelsB = {r.role for r in pB.roles}
    assert labelsA == {"Tech", "Recht", "Markt"}
    assert labelsA != labelsB  # not a constant hardcoded universal set


# ── fail-soft to skeleton, never hardcode ────────────────────────────────────────────────────────
def test_adhoc_falls_back_to_skeleton_on_emit_failure():
    reg = _registry()
    chat = FakeChat(_adhoc(_CLONES))  # distinctness validator rejects every attempt
    panel = asyncio.run(generate_adhoc_panel(
        "etwas", chat=chat, registry=reg, hint_domain="competitive", budget=3,
    ))
    assert panel.domain == "competitive"  # nearest declared panel, NOT a hardcoded set
    assert chat.calls == 3  # budget respected (1 emit + 2 reasks)


def test_adhoc_falls_back_to_skeleton_on_transport_throw():
    reg = _registry()
    chat = ThrowingChat()
    panel = asyncio.run(generate_adhoc_panel(
        "x", chat=chat, registry=reg, hint_domain="regulatory",
    ))
    assert panel.domain == "regulatory"
    assert chat.calls == 1  # transport threw once; we do not retry a transport error ourselves


def test_adhoc_raises_when_no_skeleton_and_emit_fails():
    chat = ThrowingChat()
    with pytest.raises(AdhocGenerationError):
        asyncio.run(generate_adhoc_panel("x", chat=chat, registry=None))


def test_adhoc_budget_is_capped_at_three():
    chat = FakeChat(_adhoc(_CLONES))
    reg = _registry()
    asyncio.run(generate_adhoc_panel(
        "etwas", chat=chat, registry=reg, hint_domain="competitive", budget=99,
    ))
    assert chat.calls == 3  # MAX_RETRY_BUDGET hard-caps even an oversized request


# ── nearest_panel selection ──────────────────────────────────────────────────────────────────────
def test_nearest_panel_hint_takes_precedence():
    reg = _registry()
    assert nearest_panel("völlig egal", reg, hint_domain="competitive").domain == "competitive"


def test_nearest_panel_by_content_overlap():
    reg = _registry()
    q = "Welche Reputation und Stakeholder Risiken sowie Cashflow Probleme bestehen?"
    assert nearest_panel(q, reg).domain == "risk-assessment"


def test_nearest_panel_none_without_registry():
    assert nearest_panel("x", None) is None
