"""Router happy-path snapshots (Spec 04 §11) — net-free via FakeClassifierLLM.

Known-domain runs map to the registry scaffold (effort/policy/synthesis/evidence resolved), route-floor
precedence holds (in-set hint wins; attached file forces a file route; an out-of-set hint reconciles to
the file floor with a guards_applied note).
"""
from __future__ import annotations

from _router_fakes import FakeClassifierLLM, registry, run_panel

from mpr.schema import Decision, RouterInput


def test_snapshot_architecture_decision_panel():
    reg = registry()
    arch = reg.resolve("architecture-decision")
    expected_labels = [r.role for r in arch.roles]
    llm = FakeClassifierLLM(run_panel(domain="architecture-decision", route="wide", mode="decision"))
    d = classify_helper(llm, reg, RouterInput(query="Sollen wir von Modulith auf Microservices umstellen?"))
    assert d.decision == Decision.RUN
    assert d.route.value == "wide" and d.domain == "architecture-decision" and d.mode.value == "decision"
    assert [p.role for p in d.perspectives] == expected_labels          # the 7 registry roles, in order
    assert d.synthesis_template == "decision-matrix"
    assert d.evidence_source.value == "internal"
    assert all(p.provider_policy.value == "local-only" for p in d.perspectives)
    assert llm.calls == 1


def test_snapshot_regulatory_external():
    reg = registry()
    llm = FakeClassifierLLM(run_panel(domain="regulatory", route="wide", mode="evidence-research"))
    d = classify_helper(llm, reg, RouterInput(query="Welche EU/US/UAE-Regeln gelten für Stablecoins?"))
    assert d.domain == "regulatory" and d.mode.value == "evidence-research"
    assert d.evidence_source.value == "external"
    assert d.synthesis_template == "evidence-report"
    assert all(p.provider_policy.value == "offloadable" for p in d.perspectives)


def test_snapshot_competitive_comparison():
    reg = registry()
    llm = FakeClassifierLLM(run_panel(domain="competitive", route="focused", mode="comparison"))
    d = classify_helper(llm, reg, RouterInput(query="Wie schlägt sich unser Produkt gegen X und Y?"))
    assert d.mode.value == "comparison"
    assert d.synthesis_template == "comparison-matrix"


def test_snapshot_route_hint_overrides():
    # no files, in-set hint 'focused' beats the classifier's 'wide' (P2).
    reg = registry()
    llm = FakeClassifierLLM(run_panel(domain="competitive", route="wide", mode="comparison"))
    d = classify_helper(llm, reg, RouterInput(query="Vergleiche die Anbieter", route_hint="focused"))
    assert d.route.value == "focused"


def test_snapshot_file_attached_forces_file_route():
    reg = registry()
    llm = FakeClassifierLLM(run_panel(domain="architecture-decision", route="file-augmented", mode="decision"))
    d = classify_helper(llm, reg, RouterInput(
        query="Bewerte diese Architektur und vergleiche Optionen",
        files=[{"path": "design.md", "excerpt": "..."}],
    ))
    assert d.route.value in ("file-only", "file-augmented")  # P1 floor


def test_route_hint_reconciled_to_file_floor():
    # out-of-set hint 'wide' + attached file → reconciled to file-augmented, with a note (P1 > hint).
    reg = registry()
    llm = FakeClassifierLLM(run_panel(domain="architecture-decision", route="wide", mode="decision"))
    d = classify_helper(llm, reg, RouterInput(
        query="Bewerte und vergleiche diese Architektur-Optionen sorgfältig",
        route_hint="wide", files=[{"path": "a.md", "excerpt": "x"}],
    ))
    assert d.route.value == "file-augmented"
    assert any("hint-reconciled(wide->file-augmented)" in g for g in d.guards_applied)


# local import so the module imports cleanly even if router has an error during collection
def classify_helper(llm, reg, inp):
    from mpr.router import classify
    return classify(inp, llm=llm, registry=reg)
