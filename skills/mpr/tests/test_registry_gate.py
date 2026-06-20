"""Registry collective gate (Spec 05 §10) — closes the one genuine coverage gap + a wiring smoke test.

The §10 test inventory is already satisfied across test_registry_schema/resolve/synthesis/loader/guards/
adaptive/versioning + test_start_panels (see the reconcile receipt in vault/Plan/mpr/TASKS.md). Two
additions here: (1) `test_adhoc_uses_nearest_panel_as_skeleton` — assert the nearest panel's roles
actually reach the emit prompt as a scaffold (the Hybrid step, previously only covered for selection +
fallback, never for "the scaffold is passed to the model"); (2) a public-API import smoke test so the
whole registry surface stays wired.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from mpr.registry.adaptive import generate_adhoc_panel, nearest_panel
from mpr.registry.loader import PanelRegistry

_MPR_ROOT = Path(__file__).resolve().parents[1]


def _registry() -> PanelRegistry:
    reg = PanelRegistry()
    reg.discover(_MPR_ROOT)
    return reg


def _resp(panel_dict: dict) -> dict:
    return {"choices": [{"message": {"tool_calls": [
        {"function": {"name": "emit_panel", "arguments": json.dumps(panel_dict)}}
    ]}}]}


class CapturingChat:
    """Async transport that records the prompt it was handed, then replays a fixed payload."""

    def __init__(self, payload: dict):
        self.payload = payload
        self.last_messages = None

    async def __call__(self, *, messages, model, temperature, extra_body):
        self.last_messages = messages
        return _resp(self.payload)


_VALID = {
    "domain": "adhoc", "mode": "evidence-research", "evidence_source": "mixed",
    "synthesis_template": "evidence-report", "effort_defaults": {"default": "medium"},
    "roles": [
        {"role": "Eins", "lens_prompt": "erste eigenständige Brille auf die Frage"},
        {"role": "Zwei", "lens_prompt": "zweite ganz andere Brille auf die Frage"},
        {"role": "Drei", "lens_prompt": "dritte wieder andere Brille auf die Frage"},
    ],
}


def test_adhoc_uses_nearest_panel_as_skeleton():
    reg = _registry()
    skeleton = reg.resolve("architecture-decision")
    chat = CapturingChat(_VALID)
    # hint_domain selects the scaffold deterministically; emit succeeds with _VALID.
    asyncio.run(generate_adhoc_panel(
        "Soll ich umbauen?", chat=chat, registry=reg, hint_domain="architecture-decision",
    ))
    assert chat.last_messages is not None
    user = chat.last_messages[-1]["content"]
    # the scaffold's role labels are offered to the model (Hybrid step §7.5 "1) Gerüst")
    scaffold_labels = [r.role for r in skeleton.roles]
    assert any(label in user for label in scaffold_labels), "skeleton roles not passed as scaffold"
    assert "adhoc" in user  # the domain rule reached the prompt too


def test_nearest_panel_selection_is_used_for_scaffold():
    # the scaffold offered must be exactly the nearest panel for the given query/hint.
    reg = _registry()
    assert nearest_panel("x", reg, hint_domain="competitive").domain == "competitive"


def test_registry_public_api_importable():
    # wiring smoke: the whole registry surface imports + exposes its key symbols.
    from mpr.registry import schema, resolve, synthesis, loader, guards, adaptive, config

    assert schema.Panel and schema.Role and schema.MIN_ROLES == 3
    assert resolve.resolve_effort and resolve.resolve_policy and resolve.SovereigntyError
    assert synthesis.SYNTHESIS_BINDING and synthesis.default_template_for_mode
    assert loader.PanelRegistry and loader.DuplicatePanelError and loader.get_registry
    assert guards.check_distinctness and guards.check_coverage and guards.COVERAGE_AXES
    assert adaptive.generate_adhoc_panel and adaptive.AdhocGenerationError
    assert config.RegistryConfig and config.load_registry_config
