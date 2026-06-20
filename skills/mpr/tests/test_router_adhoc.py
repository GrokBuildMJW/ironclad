"""Router adhoc path (Spec 04 §8 / §11) — the single classifier call decomposes; guards stay strict.

The router never injects a universal set: an adhoc panel is the classifier's own output, distinctness +
min-panel kill a degenerate clone-set (decline, not a generic 5er), coverage is a no-op for adhoc, and a
missing/empty registry falls to adhoc rather than crashing.
"""
from __future__ import annotations

from _router_fakes import FakeClassifierLLM, persp, registry, run_panel

from mpr.router import classify
from mpr.schema import Decision, RouterInput


def test_invalid_synthesis_template_clamped_to_valid():
    # a LIVE classifier can return a synthesis_template outside the allowed set (it is a free-text field) —
    # the router must drop it and fall back to a valid template (panel's for a known domain, mode-default
    # for adhoc), else the downstream SynthesisInput Literal rejects it (router-classify→synthesis live gap).
    from mpr.registry.schema import SynthesisTemplate
    valid = {t.value for t in SynthesisTemplate}
    d = classify(RouterInput(query="Sollen wir umbauen und die Optionen sorgfältig abwägen?"),
                 llm=FakeClassifierLLM(run_panel(domain="architecture-decision", route="wide", mode="decision",
                                                 synthesis_template="garbage-template-xyz")),
                 registry=registry())
    assert d.decision == Decision.RUN and d.synthesis_template in valid    # known → panel's valid template
    d2 = classify(RouterInput(query="Sollen wir umbauen und die Optionen abwägen und vergleichen?"),
                  llm=FakeClassifierLLM(run_panel(domain="adhoc", route="wide", mode="decision",
                                                  synthesis_template="not-a-real-template", perspectives=[
                                                      persp("Chancen", "Nutzen und Chancen abschätzen"),
                                                      persp("Risiken", "Kosten und Risiken kritisch prüfen"),
                                                      persp("Tragfähigkeit", "langfristige Reversibilität bewerten")])),
                  registry=registry())
    assert d2.decision == Decision.RUN and d2.synthesis_template in valid  # adhoc → mode default


def test_adhoc_generates_distinct_panel():
    llm = FakeClassifierLLM(run_panel(domain="adhoc", mode="evidence-research", perspectives=[
        persp("Biologe", "ökologische Folgen abschätzen"),
        persp("Ökonom", "Marktanreize und Kosten analysieren"),
        persp("Ethiker", "moralische Trade-offs beleuchten"),
    ]))
    d = classify(RouterInput(query="Sollen wir Gen-Drives gegen Malaria einsetzen?"), llm=llm, registry=registry())
    assert d.decision == Decision.RUN and d.domain == "adhoc"
    assert [p.role for p in d.perspectives] == ["Biologe", "Ökonom", "Ethiker"]  # classifier's own, no scaffold


def test_adhoc_no_universal_fallback():
    clone = "Bewerte die Gesamtlage umfassend und sorgfältig aus allgemeiner Sicht heraus"
    llm = FakeClassifierLLM(run_panel(domain="adhoc", mode="evidence-research",
                                      perspectives=[persp(f"R{i}", clone) for i in range(5)]))
    d = classify(RouterInput(query="Sollen wir das generisch bewerten oder nicht?"), llm=llm, registry=registry())
    assert d.decision == Decision.DECLINE  # clones collapse → decline, NOT a universal 5er run


def test_adhoc_coverage_noop():
    llm = FakeClassifierLLM(run_panel(domain="adhoc", mode="evidence-research", perspectives=[
        persp("Eins", "erste eigenständige Brille auf das Thema"),
        persp("Zwei", "zweite ganz andere Brille auf das Thema"),
        persp("Drei", "dritte wieder andere Brille auf das Thema"),
    ]))
    d = classify(RouterInput(query="Sollen wir diese Nische bewerten?"), llm=llm, registry=registry())
    assert d.decision == Decision.RUN
    assert not any(g.startswith("coverage:added(") for g in d.guards_applied)  # no axes for adhoc


def test_registry_unavailable_falls_to_adhoc():
    # registry=None → resolve never happens → adhoc path on the classifier's panel (R7.4).
    llm = FakeClassifierLLM(run_panel(domain="architecture-decision", mode="decision", perspectives=[
        persp("Eins", "erste eigenständige Brille"),
        persp("Zwei", "zweite andere Brille"),
        persp("Drei", "dritte wieder andere Brille"),
    ]))
    d = classify(RouterInput(query="Sollen wir diese Frage breit beleuchten?"), llm=llm, registry=None)
    assert d.decision == Decision.RUN and d.domain == "adhoc"
