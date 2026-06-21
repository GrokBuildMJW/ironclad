"""Synthesis pipeline (skills/mpr/synthesis.py) — Spec 06 §2/§6/§7 / §8-A,D. Net-free, stubbed llm.

Recorded-output synthesis per mode; deterministic input-order role binding; conservative degradation
(half-panel, insufficient quorum, synth-call exception, template parse → repair → degrade); the
write-back hands exactly one distilled insight to the injected single-writer.
"""
from __future__ import annotations

import json

from mpr.synthesis import (
    PerspectiveResult,
    SynthesisInput,
    synthesize,
    write_back,
)


def P(role, content, ok=True, error=None, provider="spark-vllm", **kw):
    return PerspectiveResult(role=role, content=content, ok=ok, error=error, provider=provider, **kw)


class StubLLM:
    def __init__(self, *bodies):
        self.bodies = list(bodies)
        self.calls = 0

    def __call__(self, prompt, *, system, max_tokens):
        i = min(self.calls, len(self.bodies) - 1)
        self.calls += 1
        return self.bodies[i]


def _throwing(prompt, *, system, max_tokens):
    raise RuntimeError("synth transport down")


_DEC = json.dumps({
    "options": ["A", "B"],
    "criteria": [{"name": "K1", "weight": 2}, {"name": "K2", "weight": 3}],
    "cells": [{"option": "A", "criterion": "K1", "score": 4}, {"option": "A", "criterion": "K2", "score": 2},
              {"option": "B", "criterion": "K1", "score": 1}, {"option": "B", "criterion": "K2", "score": 5}],
    "recommendation": "B", "recommendation_rationale": "B überwiegt", "fallback": "A",
    "fallback_trigger": "wenn X", "conflict_notes": [],
})
_EV = json.dumps({
    "summary": "Kurzfazit.",
    "findings": [{"claim": "Unbelegte Behauptung", "confidence": "high", "support": []},
                 {"claim": "Gut belegt", "confidence": "high",
                  "support": [{"role": "A", "provider": "p", "quote": "q1"},
                              {"role": "B", "provider": "p", "quote": "q2"}]}],
})
_CMP = json.dumps({
    "options": ["Wir", "Sie"],
    "criteria": [{"name": "Preis", "weight": 3}, {"name": "UX", "weight": 2}],
    "cells": [{"option": "Wir", "criterion": "Preis", "score": 4}, {"option": "Wir", "criterion": "UX", "score": 3},
              {"option": "Sie", "criterion": "Preis", "score": 2}, {"option": "Sie", "criterion": "UX", "score": 5}],
    "gaps": ["UX-Lücke"], "opportunities": ["Preis-Chance"],
})


def _inp(mode, template, perspectives, **kw):
    return SynthesisInput(run_id="r1", query="Frage?", mode=mode, synthesis_template=template,
                          domain="adhoc", evidence_source="mixed", perspectives=perspectives, **kw)


# ── §8-A recorded synthesis ──────────────────────────────────────────────────────────────────────
def test_synthesize_decision_from_recorded():
    persp = [P("R1", "Gutachten eins"), P("R2", "Gutachten zwei"), P("R3", "Gutachten drei")]
    out = synthesize(_inp("decision", "decision-matrix", persp, cross_verify=False), llm_call=StubLLM(_DEC))
    assert out.status == "full" and out.template_valid is True
    assert "Recommendation" in out.body and "Fallback" in out.body and "Weighted score" in out.body


def test_synthesize_evidence_tiers_demoted():
    persp = [P("A", "x"), P("B", "y")]
    out = synthesize(_inp("evidence-research", "evidence-report", persp, cross_verify=False),
                     llm_call=StubLLM(_EV))
    assert out.status == "full"
    low = out.body.index("### Low confidence")
    assert "Unbelegte Behauptung" in out.body[low:]   # demoted high→low


def test_synthesize_comparison_has_gaps_opportunities():
    persp = [P("A", "x"), P("B", "y")]
    out = synthesize(_inp("comparison", "comparison-matrix", persp, cross_verify=False),
                     llm_call=StubLLM(_CMP))
    assert "### Gaps" in out.body and "### Opportunities" in out.body and out.template_valid


_RISK = json.dumps({
    "summary": "Risikolage.",
    "risks": [{"risk": "SPOF", "severity": "high", "likelihood": "high", "mitigation": "HA",
               "owner": "Eng", "roles": ["Technisch"]},
              {"risk": "PR-Risiko", "severity": "low", "likelihood": "low", "mitigation": "Monitoring"}],
})


def test_synthesize_risk_register_end_to_end():
    # the risk-assessment domain (evidence-research mode + risk-register template) is functional again.
    persp = [P("Technisch", "x"), P("Operativ", "y"), P("Reputation", "z")]
    out = synthesize(_inp("evidence-research", "risk-register", persp, cross_verify=False),
                     llm_call=StubLLM(_RISK))
    assert out.status == "full" and out.template_valid is True
    assert "| Risk | Severity | Likelihood | Mitigation | Owner |" in out.body
    assert out.body.index("SPOF") < out.body.index("PR-Risiko")   # worst-first


def test_input_order_role_binding():
    persp = [P("Erste", "a"), P("Zweite", "b"), P("Dritte", "c")]
    out = synthesize(_inp("decision", "decision-matrix", persp, cross_verify=False), llm_call=StubLLM(_DEC))
    assert out.used == ["Erste", "Zweite", "Dritte"]   # panel/input order preserved


# ── §8-D degradation ─────────────────────────────────────────────────────────────────────────────
def test_degraded_when_half_panel_fails():
    persp = [P("A", "a"), P("B", "b"), P("C", None, ok=False, error="timeout"),
             P("D", None, ok=False, error="empty")]
    out = synthesize(_inp("decision", "decision-matrix", persp, cross_verify=False), llm_call=StubLLM(_DEC))
    assert out.status == "degraded"
    assert "Panel incomplete" in out.body
    assert {d["role"] for d in out.dropped} == {"C", "D"}


def test_insufficient_quorum_no_pseudo_synthesis():
    llm = StubLLM(_DEC)
    persp = [P("A", "die einzige Sicht"), P("B", None, ok=False, error="x"), P("C", None, ok=False, error="y")]
    out = synthesize(_inp("decision", "decision-matrix", persp), llm_call=llm)
    assert out.status == "degraded"
    assert "Too few perspectives" in out.body and "die einzige Sicht" in out.body
    assert llm.calls == 0   # no pseudo-synthesis call


def test_single_perspective_is_insufficient_no_call():
    # H1 regression: n=1, k=1 must be insufficient (verbatim single view), NOT a full pseudo-synthesis.
    llm = StubLLM(_DEC)
    out = synthesize(_inp("decision", "decision-matrix", [P("R1", "nur eine Sicht")]), llm_call=llm)
    assert out.status == "degraded"
    assert "Too few perspectives" in out.body and "nur eine Sicht" in out.body
    assert llm.calls == 0   # no synthesis call for a single lens


def test_synth_call_exception_degrades():
    persp = [P("A", "a"), P("B", "b"), P("C", "c")]
    out = synthesize(_inp("decision", "decision-matrix", persp, cross_verify=False), llm_call=_throwing)
    assert out.status == "degraded" and "Synthesis degraded" in out.body  # no raise


def test_template_parse_repair_then_success():
    persp = [P("A", "a"), P("B", "b"), P("C", "c")]
    llm = StubLLM("kein json", _DEC)   # first invalid → repair re-ask → valid
    out = synthesize(_inp("decision", "decision-matrix", persp, cross_verify=False), llm_call=llm)
    assert out.status == "full" and out.template_valid is True
    assert llm.calls == 2   # exactly one repair re-ask


def test_template_parse_repair_then_degrade():
    persp = [P("A", "a"), P("B", "b"), P("C", "c")]
    llm = StubLLM("kein json", "auch kein json")   # both invalid → degrade
    out = synthesize(_inp("decision", "decision-matrix", persp, cross_verify=False), llm_call=llm)
    assert out.status == "degraded" and out.template_valid is False
    assert llm.calls == 2


# ── §6.2 write-back ─────────────────────────────────────────────────────────────────────────────
class _Reducer:
    def __init__(self):
        self.calls = []

    def __call__(self, entries, *, topic):
        self.calls.append((entries, topic))
        return len(entries)


def test_writeback_uses_single_writer():
    persp = [P("A", "a"), P("B", "b"), P("C", "c")]
    inp = _inp("decision", "decision-matrix", persp, cross_verify=False)
    out = synthesize(inp, llm_call=StubLLM(_DEC))
    red = _Reducer()
    write_back(out, inp, red)
    assert len(red.calls) == 1                       # exactly one write
    entries, topic = red.calls[0]
    assert len(entries) == 1 and entries[0]["ok"] is True   # exactly one distilled entry
    assert topic.startswith("MPR decision:")


def test_writeback_disabled_is_noop():
    persp = [P("A", "a"), P("B", "b"), P("C", "c")]
    inp = _inp("decision", "decision-matrix", persp, cross_verify=False)
    out = synthesize(inp, llm_call=StubLLM(_DEC))
    assert write_back(out, inp, None) is None        # reducer None → no-op, no raise
