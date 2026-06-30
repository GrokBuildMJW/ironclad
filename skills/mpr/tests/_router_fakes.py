"""Shared test doubles for the router suite (not collected — no test_ prefix).

A net-free ``FakeClassifierLLM`` (records JSON replies + counts calls) and a discovered PanelRegistry.
Importable as a sibling module because pytest puts each test file's directory on sys.path.
"""
from __future__ import annotations

import json
from pathlib import Path

from mpr.registry.loader import PanelRegistry

_MPR_ROOT = Path(__file__).resolve().parents[1]


def registry() -> PanelRegistry:
    reg = PanelRegistry()
    reg.discover(_MPR_ROOT)
    return reg


class FakeClassifierLLM:
    """Replays the given responses (dict→json or raw str); the last repeats. Counts calls."""

    def __init__(self, *responses, raises: bool = False):
        self.responses = [r if isinstance(r, str) else json.dumps(r) for r in responses]
        self.raises = raises
        self.calls = 0

    def complete_json(self, system, user, *, max_tokens, temperature) -> str:
        self.calls += 1
        if self.raises:
            raise RuntimeError("classifier transport down")
        if not self.responses:
            return "{}"
        return self.responses[min(self.calls - 1, len(self.responses) - 1)]


def run_panel(domain="adhoc", route="wide", mode="decision", perspectives=None,
              synthesis_template=None, evidence_source=None, **extra) -> dict:
    d = {"decision": "run", "route": route, "domain": domain, "mode": mode,
         "perspectives": perspectives if perspectives is not None else []}
    if synthesis_template is not None:
        d["synthesis_template"] = synthesis_template
    if evidence_source is not None:
        d["evidence_source"] = evidence_source
    d.update(extra)
    return d


def persp(role, lens=None, **extra) -> dict:
    p = {"role": role, "lens_prompt": lens or f"lens for {role}"}
    p.update(extra)
    return p
