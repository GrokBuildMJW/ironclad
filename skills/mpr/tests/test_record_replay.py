"""Record/Replay against a frozen golden recording (Spec 08 §4).

The run-manifest IS the fixture: ``eval/recordings/REC-arch-0001/`` was generated ONCE from the real
synthesize+audit+classify path and committed. These tests replay sub-stages OFFLINE (no fan-out, no
model — only an injected stub llm_call) and assert byte-reproduction of the frozen artifacts, so a
synthesis/template/render regression OR a manifest-schema break is caught by a fixture mismatch.

Regenerate the fixture only on an intentional format change (the generator is documented in the Ev-4
TASKS.md receipt). The fixture is a decision-matrix run (adhoc, internal → all local-only, 0 egress).
"""
from __future__ import annotations

import json
from pathlib import Path

from _router_fakes import FakeClassifierLLM, registry

from mpr.audit import Manifest
from mpr.router import classify
from mpr.schema import RouterInput
from mpr.synthesis import PerspectiveResult, SynthesisInput, synthesize

_REC = Path(__file__).resolve().parents[1] / "eval" / "recordings" / "REC-arch-0001"


def _read(name):
    return (_REC / name).read_text(encoding="utf-8")


def _recorded_body():
    """The golden synthesis body = synthesis.md minus the `<!-- … -->` header write_synthesis prepends."""
    return _read("synthesis.md").split("-->\n\n", 1)[1]


def _replay_input():
    ri = json.loads(_read("replay_inputs.json"))
    presults = [PerspectiveResult(role=p["role"], ok=True, content=p["content"], error=None,
                                  provider=p["provider"], effort=p["effort"],
                                  provider_policy=p["provider_policy"], completion_tokens=120, latency=0.1)
                for p in ri["perspectives"]]
    inp = SynthesisInput(run_id=ri["run_id"], query=ri["query"], mode=ri["mode"],
                         synthesis_template=ri["synthesis_template"], domain=ri["domain"],
                         evidence_source=ri["evidence_source"], perspectives=presults)
    return inp, ri["synthesis_json"]


# ── §4 recording_roundtrip ───────────────────────────────────────────────────────────────────────
def test_recording_roundtrip():
    m = Manifest.model_validate_json(_read("manifest.json"))
    assert Manifest.model_validate_json(m.model_dump_json()) == m          # lossless, serialization-stable
    assert m.run_id == "REC-arch-0001" and m.status == "ok"
    assert m.sovereignty_summary.violations == 0 and m.provenance.egress == []   # frozen sovereign proof
    assert all(p.provider_policy == "local-only" and p.substrate == "in-engine" for p in m.perspectives)


# ── §4 replay synthesis from recorded perspectives (deterministic, offline) ───────────────────────
def test_replay_synthesis_from_recorded_perspectives():
    inp, synth_json = _replay_input()
    out = synthesize(inp, llm_call=lambda prompt, *, system, max_tokens: json.dumps(synth_json))
    assert out.status == "full" and out.template_valid
    assert out.body == _recorded_body()                                   # byte-reproduces the golden


def test_replay_is_offline():
    inp, synth_json = _replay_input()
    calls = {"n": 0}

    def _stub(prompt, *, system, max_tokens):
        calls["n"] += 1
        return json.dumps(synth_json)

    out = synthesize(inp, llm_call=_stub)
    assert calls["n"] == 1                                                # exactly one injected call…
    assert out.body == _recorded_body()                                  # …no fan-out, no network reached


def test_replay_detects_synthesis_regression():
    # a perturbed synthesis JSON (flipped recommendation) MUST diverge from the frozen golden — proves the
    # replay is sensitive (a real synthesis/render regression would surface the same way).
    inp, synth_json = _replay_input()
    perturbed = dict(synth_json, recommendation="In Microservices aufteilen")
    out = synthesize(inp, llm_call=lambda prompt, *, system, max_tokens: json.dumps(perturbed))
    assert out.body != _recorded_body()


# ── §4 replay router decision (deterministic from recorded classifier reply) ──────────────────────
def test_replay_router_decision():
    reply = json.loads(_read("classifier_reply.json"))
    golden = json.loads(_read("router_decision.json"))
    query = json.loads(_read("replay_inputs.json"))["query"]             # the canonical recorded query
    decision = classify(RouterInput(query=query), llm=FakeClassifierLLM(reply), registry=registry())
    assert json.loads(decision.model_dump_json()) == golden              # byte-stable, reproduced exactly
