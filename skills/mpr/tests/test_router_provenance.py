"""Router audit hook (Spec 04 §9) — provenance fields for the run-manifest (1d).

The router writes no manifest; it supplies the §9 fields. provenance() is a JSON-safe dump carrying
all PROVENANCE_FIELDS; the §9 invariant holds: classifier_raw is None ⇔ a pre-check decline.
"""
from __future__ import annotations

from _router_fakes import FakeClassifierLLM, registry, run_panel

from mpr.router import PROVENANCE_FIELDS, classify, provenance
from mpr.schema import Decision, RouterInput


def test_provenance_has_all_fields_on_run():
    d = classify(RouterInput(query="Sollen wir von Modulith auf Microservices umstellen?"),
                 llm=FakeClassifierLLM(run_panel(domain="architecture-decision", route="wide", mode="decision")),
                 registry=registry())
    prov = provenance(d)
    assert set(PROVENANCE_FIELDS).issubset(prov.keys())
    assert prov["decision"] == "run" and prov["domain"] == "architecture-decision"
    assert prov["perspectives"] and "role" in prov["perspectives"][0]
    assert "effort" in prov["perspectives"][0] and "provider_policy" in prov["perspectives"][0]
    assert prov["classifier_raw"] is not None  # an LLM call happened


def test_precheck_decline_has_null_classifier_raw():
    # §9 invariant: a pre-check decline made no LLM call → classifier_raw is None (expected, not missing).
    llm = FakeClassifierLLM(run_panel())
    d = classify(RouterInput(query="Was ist die Hauptstadt von Frankreich?"), llm=llm, registry=registry())
    assert d.decision == Decision.DECLINE
    prov = provenance(d)
    assert prov["classifier_raw"] is None
    assert any(g.startswith("precheck:") for g in prov["guards_applied"])
    assert llm.calls == 0


def test_llm_path_decline_carries_classifier_raw():
    # a decline AFTER the call (unparsable) keeps the raw output for audit/replay.
    llm = FakeClassifierLLM("not json", "still not json")
    d = classify(RouterInput(query="Sollen wir A oder B wählen und warum?"), llm=llm, registry=registry())
    assert d.decision == Decision.DECLINE
    prov = provenance(d)
    assert prov["classifier_raw"] is not None
    assert not any(g.startswith("precheck:") for g in prov["guards_applied"])


def test_classifier_raw_none_iff_precheck():
    # the biconditional the manifest/replay relies on, checked across a precheck + an LLM-path decision.
    reg = registry()
    precheck = classify(RouterInput(query="Was ist 2+2?"), llm=FakeClassifierLLM(run_panel()), registry=reg)
    ran = classify(RouterInput(query="Sollen wir umbauen und Optionen vergleichen?"),
                   llm=FakeClassifierLLM(run_panel(domain="architecture-decision", route="wide", mode="decision")),
                   registry=reg)
    assert (provenance(precheck)["classifier_raw"] is None) == any(
        g.startswith("precheck:") for g in precheck.guards_applied)
    assert (provenance(ran)["classifier_raw"] is None) == any(
        g.startswith("precheck:") for g in ran.guards_applied)
