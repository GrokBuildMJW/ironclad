"""Router decline criteria (Spec 04 §4 / §7 / §11) — net-free.

Cheap pre-check declines fire BEFORE the LLM call (asserted via call count == 0); a deliberation marker
vetoes the single-fact/yes-no rules; unparsable/throwing classifier degrades to a typed decline; a
classifier decline is never upgraded to run.
"""
from __future__ import annotations

from _router_fakes import FakeClassifierLLM, persp, registry, run_panel

from mpr.router import classify
from mpr.schema import Decision, RouterInput


def test_decline_single_fact_no_llm_call():
    llm = FakeClassifierLLM(run_panel())  # would be used IF the call happened
    d = classify(RouterInput(query="Was ist die Hauptstadt von Frankreich?"), llm=llm, registry=registry())
    assert d.decision == Decision.DECLINE
    assert d.decline_reason == "single-fact lookup"
    assert llm.calls == 0  # pre-check R2 short-circuits — NO classifier call


def test_decline_too_short():
    llm = FakeClassifierLLM(run_panel())
    d = classify(RouterInput(query="status?"), llm=llm, registry=registry())
    assert d.decision == Decision.DECLINE and d.decline_reason == "too short for multi-lens analysis"
    assert llm.calls == 0


def test_decline_single_source_extractive():
    llm = FakeClassifierLLM(run_panel())
    d = classify(
        RouterInput(query="Extrahiere die Kennzahlen aus dem Dokument",
                    files=[{"path": "report.pdf", "excerpt": "..."}]),
        llm=llm, registry=registry(),
    )
    assert d.decision == Decision.DECLINE
    assert d.decline_reason == "single-source retrieval — no multi-perspective gain"
    assert llm.calls == 0


def test_decline_yesno_fact():
    llm = FakeClassifierLLM(run_panel())
    d = classify(RouterInput(query="Ist Python eine kompilierte Sprache?"), llm=llm, registry=registry())
    assert d.decision == Decision.DECLINE and d.decline_reason == "closed factual question"
    assert llm.calls == 0


def test_no_decline_when_deliberation_marker():
    # "best" vetoes the single-fact rule → the call proceeds and a run is produced.
    llm = FakeClassifierLLM(run_panel(
        domain="adhoc", mode="evidence-research",
        perspectives=[persp("Performance", "Durchsatz und Latenz bewerten"),
                      persp("Kosten", "Total cost of ownership prüfen"),
                      persp("Betrieb", "Betreibbarkeit und Wartung beurteilen")],
    ))
    d = classify(RouterInput(query="What is the best database for high-throughput writes?"),
                 llm=llm, registry=registry())
    assert d.decision == Decision.RUN
    assert llm.calls == 1


def test_no_decline_when_german_best_marker():
    # H1 regression: German attributive "beste" must veto the single-fact rule (the MPR core case).
    llm = FakeClassifierLLM(run_panel(
        domain="adhoc", mode="evidence-research",
        perspectives=[persp("Performance", "Durchsatz und Latenz beurteilen"),
                      persp("Kosten", "Betriebskosten prüfen"),
                      persp("Betrieb", "Wartbarkeit beurteilen")],
    ))
    d = classify(RouterInput(query="Was ist die beste Datenbank für hohe Schreiblast?"),
                 llm=llm, registry=registry())
    assert d.decision == Decision.RUN
    assert llm.calls == 1


def test_decline_on_unparsable_classifier():
    llm = FakeClassifierLLM("I cannot answer that.", "still not JSON")  # 1 emit + 1 reask
    d = classify(RouterInput(query="Sollen wir A oder B wählen und warum?"), llm=llm, registry=registry())
    assert d.decision == Decision.DECLINE and d.decline_reason == "router-classify-failed"
    assert llm.calls == 2  # one re-ask, then decline


def test_decline_on_llm_exception():
    llm = FakeClassifierLLM(raises=True)
    d = classify(RouterInput(query="Sollen wir umbauen und welche Risiken entstehen?"),
                 llm=llm, registry=registry())
    assert d.decision == Decision.DECLINE and d.decline_reason == "router-llm-unavailable"
    assert llm.calls == 1


def test_decline_never_upgraded_to_run():
    llm = FakeClassifierLLM({"decision": "decline", "decline_reason": "not worth fanning out",
                             "perspectives": [persp("X"), persp("Y"), persp("Z")]})
    d = classify(RouterInput(query="Sollen wir das wirklich vergleichen?"), llm=llm, registry=registry())
    assert d.decision == Decision.DECLINE
    assert d.decline_reason == "not worth fanning out"
