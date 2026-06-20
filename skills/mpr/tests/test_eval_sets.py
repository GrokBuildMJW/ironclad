"""Ev-8 starter calibration data (Spec 08 §5.3/§10) — eval-sets + refs validity + no-over-decline.

User-chosen path: SPEC-SEEDED STARTER sets (≥8 queries/domain) to refine with real target questions later;
DEFAULT_POOL pricing; judge panel Sonnet·Opus·Spark. These tests pin the data's VALIDITY (well-formed,
unique ids, refs cover every query, refs axes == the panel COVERAGE_AXES) and its QUALITY (every run-domain
query is deliberative enough to pass the router pre-check → classifies to run, never an over-decline).
"""
from __future__ import annotations

import importlib.util
import json
import tomllib
from pathlib import Path

from _router_fakes import FakeClassifierLLM, persp, registry, run_panel

from mpr.registry.guards import COVERAGE_AXES
from mpr.router import classify
from mpr.schema import RouterInput

_EVAL = Path(__file__).resolve().parents[1] / "eval"

# domain slug → (set file, refs file, classifier-reply shape for the no-over-decline check)
_DOMAINS = {
    "architecture-decision": ("architecture_decision", dict(mode="decision", synthesis_template="decision-matrix", evidence_source="internal")),
    "regulatory": ("regulatory", dict(mode="evidence-research", synthesis_template="evidence-report", evidence_source="external")),
    "competitive": ("competitive", dict(mode="comparison", synthesis_template="comparison-matrix", evidence_source="external")),
    "risk-assessment": ("risk_assessment", dict(mode="evidence-research", synthesis_template="risk-register", evidence_source="mixed")),
    "adhoc": ("adhoc", dict(mode="decision", synthesis_template="decision-matrix", evidence_source="mixed")),
}


def _set(stem):
    lines = (_EVAL / "sets" / f"{stem}.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(ln) for ln in lines if ln.strip()]


def _refs(stem):
    return json.loads((_EVAL / "refs" / f"{stem}.refs.json").read_text(encoding="utf-8"))


def _reply(domain, spec):
    kw = dict(domain=domain, route="wide", **spec)
    if domain == "adhoc":                      # adhoc has no registry scaffold → supply distinct lenses
        kw["perspectives"] = [
            persp("Chancen", lens="Analysiere Nutzen und Chancen der Optionen.", effort="medium", provider_policy="offloadable"),
            persp("Risiken", lens="Bewerte Kosten, Risiken und Aufwand kritisch.", effort="medium", provider_policy="offloadable"),
            persp("Tragfähigkeit", lens="Prüfe langfristige Tragfähigkeit und Reversibilität.", effort="high", provider_policy="offloadable")]
    return run_panel(**kw)


def test_each_domain_set_well_formed():
    for domain, (stem, _spec) in _DOMAINS.items():
        rows = _set(stem)
        assert len(rows) >= 8, f"{domain}: only {len(rows)} queries (Spec §10: ≥8)"
        ids = [r["id"] for r in rows]
        assert len(ids) == len(set(ids)), f"{domain}: duplicate ids"
        for r in rows:
            assert r.get("query", "").strip() and r.get("domain") == domain and r.get("id")


def test_refs_cover_every_query_with_axes():
    for domain, (stem, _spec) in _DOMAINS.items():
        rows, refs = _set(stem), _refs(stem)
        for r in rows:
            entry = refs.get(r["id"])
            assert entry and entry.get("axes"), f"{domain}: {r['id']} missing refs/axes"


def test_known_domain_refs_match_coverage_axes():
    # refs ground-truth axes for the 4 panel-backed domains MUST equal the panels' COVERAGE_AXES (so the
    # judge scores coverage against exactly what the panel is built to cover). adhoc is curated separately.
    for domain, (stem, _spec) in _DOMAINS.items():
        if domain not in COVERAGE_AXES:
            continue
        for qid, entry in _refs(stem).items():
            assert set(entry["axes"]) == set(COVERAGE_AXES[domain]), f"{domain}/{qid} axes drift"


def test_eval_queries_do_not_over_decline():
    # every run-domain query must be deliberative enough to pass the pre-check and classify to run
    # (a query the router would decline is a bad 'run' eval query). Deterministic: recorded run reply.
    for domain, (stem, spec) in _DOMAINS.items():
        reply = _reply(domain, spec)
        for r in _set(stem):
            d = classify(RouterInput(query=r["query"]), llm=FakeClassifierLLM(reply), registry=registry())
            assert d.decision.value == "run", f"{r['id']} over-declined: {r['query']!r} → {d.decline_reason}"


def test_judge_panel_configured_three_voices():
    gate = tomllib.loads((_EVAL / "gate.toml").read_text(encoding="utf-8"))
    assert gate["judge"]["panel"] == ["claude-sonnet", "claude-opus", "spark-vllm"]
    assert gate["judge"]["self_consistency_tol"] == 1.0
