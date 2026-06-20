"""Router replay determinism (Spec 04 §11c) — a recorded classifier reply yields a byte-stable decision.

The single LLM call is the only nondeterministic input; given the same recorded JSON, classify's
deterministic floor/coerce/guards must produce an identical RouterDecision dump on every run.
"""
from __future__ import annotations

from _router_fakes import FakeClassifierLLM, persp, registry, run_panel

from mpr.router import classify
from mpr.schema import RouterInput

_RECORDED = run_panel(domain="architecture-decision", route="wide", mode="decision")


def test_replay_recorded_decision_is_byte_stable():
    reg = registry()
    inp = RouterInput(query="Sollen wir von Modulith auf Microservices umstellen?")
    d1 = classify(inp, llm=FakeClassifierLLM(_RECORDED), registry=reg)
    d2 = classify(inp, llm=FakeClassifierLLM(_RECORDED), registry=reg)
    assert d1.model_dump_json() == d2.model_dump_json()


def test_replay_adhoc_decision_is_byte_stable():
    reg = registry()
    recorded = run_panel(domain="adhoc", mode="evidence-research", perspectives=[
        persp("Eins", "erste eigenständige Brille"), persp("Zwei", "zweite andere Brille"),
        persp("Drei", "dritte wieder andere Brille"),
    ])
    inp = RouterInput(query="Sollen wir diese Nische breit bewerten?")
    d1 = classify(inp, llm=FakeClassifierLLM(recorded), registry=reg)
    d2 = classify(inp, llm=FakeClassifierLLM(recorded), registry=reg)
    assert d1.model_dump_json() == d2.model_dump_json()
