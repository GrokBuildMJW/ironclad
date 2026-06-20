"""Router guards (Spec 04 §6 / §5.5 / §11) — distinctness, coverage, min-panel, sovereignty clamp.

Net-free. Distinctness drops paraphrase clones (and caps at MAX_PANEL); coverage pulls a missing axis'
registry role; a too-thin panel declines; the internal-evidence clamp forces every perspective
local-only (verified at the coerce layer so an LLM-proposed offloadable role is provably overridden).
"""
from __future__ import annotations

import pytest
from _router_fakes import FakeClassifierLLM, persp, registry, run_panel

from mpr.router import (
    _Params,
    _coerce_and_default,
    _coverage_guard,
    _route_from_files_and_hint,
    classify,
)
from mpr.schema import Decision, Mode, Perspective, Route, RouterDecision, RouterInput
from pydantic import ValidationError


def test_distinctness_drops_paraphrase():
    same = "Bewerte die wirtschaftlichen Folgen dieser Entscheidung sehr genau und sorgfältig"
    llm = FakeClassifierLLM(run_panel(domain="adhoc", mode="evidence-research", perspectives=[
        persp("A", same), persp("B", same),  # exact clones → jaccard 1.0
        persp("Technik", "Architektur Skalierung und Stabilität prüfen"),
        persp("Recht", "Regulatorische Vorgaben und Lizenzen untersuchen"),
    ]))
    d = classify(RouterInput(query="Sollen wir investieren und welche Risiken?"), llm=llm, registry=registry())
    assert d.decision == Decision.RUN
    assert any(g.startswith("distinctness:dropped(") for g in d.guards_applied)
    roles = [p.role for p in d.perspectives]
    assert ("A" in roles) ^ ("B" in roles)  # exactly one clone survived


def test_max_panel_capped():
    topics = ["Sicherheit Angriffsfläche", "Performance Latenz Durchsatz", "Kosten Budget Lizenz",
              "Recht Compliance Vorgaben", "Markt Wettbewerb Nachfrage", "Team Personal Lernkurve",
              "Betrieb Wartung Monitoring", "Daten Qualität Integrität", "Roadmap Zukunft Vision",
              "Reputation Vertrauen Öffentlichkeit"]
    many = [persp(f"R{i}", t) for i, t in enumerate(topics)]  # 10 genuinely distinct lenses
    llm = FakeClassifierLLM(run_panel(domain="adhoc", mode="evidence-research", perspectives=many))
    d = classify(RouterInput(query="Sollen wir das umfassend bewerten?"), llm=llm, registry=registry())
    assert d.decision == Decision.RUN
    assert len(d.perspectives) == 7  # MAX_PANEL


def test_min_panel_declines():
    llm = FakeClassifierLLM(run_panel(domain="adhoc", mode="evidence-research", perspectives=[
        persp("Eins", "erste eigenständige Brille"), persp("Zwei", "zweite andere Brille"),
    ]))  # 2 distinct → below MIN_PANEL=3
    d = classify(RouterInput(query="Sollen wir abwägen zwischen A und B?"), llm=llm, registry=registry())
    assert d.decision == Decision.DECLINE
    assert d.decline_reason == "insufficient distinct perspectives (2<3)"


def test_coverage_adds_missing_axis():
    reg = registry()
    cand = RouterDecision(
        decision=Decision.RUN, route=Route.WIDE, domain="architecture-decision", mode=Mode.DECISION,
        perspectives=[
            Perspective(role="Sec", lens_prompt="Security Zero-Trust Angriffsfläche"),
            Perspective(role="Perf", lens_prompt="Performance Durchsatz Latenz unter Last"),
            Perspective(role="Cost", lens_prompt="Kosten TCO Lizenz Lebenszyklus"),
        ],
        synthesis_template="decision-matrix", evidence_source="internal",
    )
    out = _coverage_guard(cand, RouterInput(query="x"), reg, _Params())
    assert len(out.perspectives) > 3                               # missing axes pulled in
    assert any(g.startswith("coverage:added(") for g in out.guards_applied)
    # the registry roles the guard pulled in (beyond the 3 seeded) are clamped local-only (internal).
    assert all(p.provider_policy.value == "local-only" for p in out.perspectives[3:])


def test_internal_forces_local_only():
    # an LLM-proposed offloadable role on an internal domain is clamped (verified pre-cap, at coerce).
    reg = registry()
    inp = RouterInput(query="Bewerte und vergleiche diese internen Architektur-Optionen")
    floor = _route_from_files_and_hint(inp)
    raw = run_panel(domain="architecture-decision", route="wide", mode="decision",
                    perspectives=[persp("Leaky", "will nach außen", provider_policy="offloadable")])
    import json
    cand = _coerce_and_default(json.dumps(raw), inp, floor, reg)
    assert cand.evidence_source.value == "internal"
    assert all(p.provider_policy.value == "local-only" for p in cand.perspectives)
    assert any(p.role == "Leaky" for p in cand.perspectives)  # the offloadable extra survived + clamped


def test_model_validator_rejects_run_without_panel():
    with pytest.raises(ValidationError):
        RouterDecision(decision=Decision.RUN, route=Route.WIDE, domain="x", mode=Mode.DECISION,
                       perspectives=[])


def test_guard_exception_degrades_to_decline(monkeypatch):
    # M1: a guard fault (here a forced raise) must degrade to decline, never escape classify.
    import mpr.router as R

    def _boom(*a, **k):
        raise RuntimeError("guard blew up")

    monkeypatch.setattr(R, "_coverage_guard", _boom)
    llm = FakeClassifierLLM(run_panel(domain="architecture-decision", route="wide", mode="decision"))
    d = R.classify(RouterInput(query="Sollen wir umbauen und welche Optionen vergleichen?"),
                   llm=llm, registry=registry())
    assert d.decision == Decision.DECLINE and d.decline_reason == "router-guard-failed"
