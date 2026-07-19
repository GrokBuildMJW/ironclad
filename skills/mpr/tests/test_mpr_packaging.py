"""MPR plugin packaging + integration (Spec 10 §3/§4/§5/§6 + §8 fail-soft + Syn-8-§8-E + Aud-9-§11e).

Contract: CASE (unique capability, NOT-FOR gate, sentinel instr), derived tool-schema (query required,
grammar-clean), sync run, A/B flag gate. Integration: the full run_mpr orchestration over stubs writes a
manifest (sovereignty violations 0, all local for an internal domain), indexes the task, mirrors one
insight, and returns the report between sentinels; decline short-circuits; every fault degrades to a
string (run never raises).
"""
from __future__ import annotations

import importlib
import importlib.util
import inspect
import json
from pathlib import Path

from _router_fakes import FakeClassifierLLM, persp, registry, run_panel
from ack.registry import derive_tool_schema

from mpr.entry import Deps, build_case, mpr_research_run, run_mpr
from mpr.mpr_config import DEFAULT_POOL, DEFAULT_ROUTING
from mpr.registry.resolve import EFFORT_MAX_TOKENS

_DEC = json.dumps({
    "options": ["A", "B"], "criteria": [{"name": "K1", "weight": 2}, {"name": "K2", "weight": 3}],
    "cells": [{"option": "A", "criterion": "K1", "score": 4}, {"option": "A", "criterion": "K2", "score": 2},
              {"option": "B", "criterion": "K1", "score": 1}, {"option": "B", "criterion": "K2", "score": 5}],
    "recommendation": "B", "recommendation_rationale": "B überwiegt", "fallback": "A",
    "fallback_trigger": "wenn X", "conflict_notes": [],
})


def _writer(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class _FakeStore:
    def __init__(self):
        self.created = []

    def create(self, fields, *, force=False, now_iso=None):
        self.created.append((dict(fields), force))
        return {**fields, "id": f"KGC-{len(self.created):03d}"}

    def transition(self, tid, to):
        return {"id": tid, "status": to}


def _deps(tmp_path, llm, *, store=None, reducer=None, run_id="mpr-test-0001",
          fanout=None) -> Deps:
    return Deps(
        llm=llm, registry=registry(),
        fanout=fanout or (lambda prompts, *, system, max_tokens, think=True: [
            {"ok": True, "content": f"Gutachten {i}", "error": None, "completion_tokens": 100,
             "latency": 0.1} for i, _ in enumerate(prompts)]),
        synth_llm=lambda p, *, system, max_tokens: _DEC,
        reducer=reducer, store=store, writer=_writer, run_id=run_id, runs_dir=str(tmp_path),
        audit_level="full-per-perspective", pool=dict(DEFAULT_POOL), routing=dict(DEFAULT_ROUTING),
        default_offload="claude-sonnet",
        sovereignty={"default_policy": "offloadable", "internal_is_local_only": True, "fail_closed": True})


# ── §3 CASE + §6 schema + §4 sync (the contract) ──────────────────────────────────────────────────
def test_case_has_unique_capability_and_gate():
    c = build_case()
    assert c["capability"] == "mpr_research" == c["name"]
    assert "NOT FOR" in c["description"]                       # the layer-1 gate
    assert "<<<MPR_REPORT>>>" in c["description"]                # sentinel instruction (§6.1)


def test_derived_schema_only_query_required_and_clean():
    schema = derive_tool_schema(mpr_research_run)
    props = schema.get("properties") or schema.get("parameters", {}).get("properties", {})
    required = schema.get("required") or schema.get("parameters", {}).get("required", [])
    assert props["query"]["type"] == "string"
    # #1535: `files: Optional[List[str]]` keeps its null arm — the value schema is `anyOf: [array, null]`
    # (matching pydantic's Optional rendering), so passing None is valid; it is optional via its default.
    assert props["files"] == {"anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "null"}]}
    assert required == ["query"]                                 # only query required (no default)
    addl = schema.get("additionalProperties", schema.get("parameters", {}).get("additionalProperties"))
    assert addl is False                                         # grammar-clean, no invented keys


def test_run_is_sync():
    assert not inspect.iscoroutinefunction(mpr_research_run)


# ── core built-in: always exports the tool (ADR-0002 #115 — no load gate) ──────────────────────────
def test_standalone_always_exports_tool():
    """MPR is a core built-in now: the standalone entry ALWAYS exports CASE + run (no GX10_MPR
    load gate). The live on/off is the runtime config `mpr.enabled` (tested via run() below)."""
    import mpr.entry as E
    path = Path(E.__file__).resolve().parent / "skills" / "mpr_research.py"
    spec = importlib.util.spec_from_file_location("_mpr_standalone_probe", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert hasattr(mod, "CASE") and hasattr(mod, "run")
    assert mod.CASE["capability"] == "mpr_research"


# ── §4 fail-soft + decline ────────────────────────────────────────────────────────────────────────
def test_empty_query_returns_error_string():
    assert run_mpr("   ", deps=_deps(Path("."), FakeClassifierLLM(run_panel()))).startswith("ERROR: mpr_research")


def test_decline_short_circuits_no_sentinels(tmp_path):
    llm = FakeClassifierLLM(run_panel())   # would be used if the call happened
    out = run_mpr("Was ist die Hauptstadt von Frankreich?", deps=_deps(tmp_path, llm))
    assert out.startswith("MPR declined") and "<<<MPR_REPORT>>>" not in out
    assert llm.calls == 0 and not list(tmp_path.glob("mpr-*"))   # precheck → no call, no run-dir


def test_disabled_runtime_gate_returns_note(tmp_path):
    # runtime active-gate off → clean sentinel-free note, no panel/run (the in-session /config set toggle).
    llm = FakeClassifierLLM(run_panel(domain="architecture-decision", route="wide", mode="decision"))
    deps = _deps(tmp_path, llm)
    deps.enabled = False
    out = run_mpr("Sollen wir umbauen und die Optionen abwägen?", deps=deps)
    assert out.startswith("MPR is disabled") and "/config set mpr.enabled on" in out
    assert "<<<MPR_REPORT>>>" not in out and llm.calls == 0   # no classify, no work when disabled


def test_run_never_raises_on_dispatch_fault(tmp_path):
    def _boom(prompts, *, system, max_tokens, think=True):
        raise RuntimeError("fanout down")
    llm = FakeClassifierLLM(run_panel(domain="architecture-decision", route="wide", mode="decision"))
    out = run_mpr("Sollen wir umbauen und Optionen vergleichen?", deps=_deps(tmp_path, llm, fanout=_boom))
    assert out.startswith("ERROR: mpr_research: dispatch")        # degraded, no raise


# ── full pipeline (§8-E sentinels + §11e manifest) ─────────────────────────────────────────────────
def test_full_pipeline_sentinels_manifest_sovereign(tmp_path):
    store, calls = _FakeStore(), []
    llm = FakeClassifierLLM(run_panel(domain="architecture-decision", route="wide", mode="decision"))
    deps = _deps(tmp_path, llm, store=store, reducer=lambda items, *, topic: (calls.append((items, topic)) or 1))
    out = run_mpr("Sollen wir von Modulith auf Microservices umstellen?", deps=deps)
    # §8-E: report between sentinels
    assert out.startswith("<<<MPR_REPORT>>>") and out.rstrip().endswith("<<<END>>>")
    assert "Recommendation" in out
    # §11e: manifest written + sovereign (architecture-decision is internal → all local, 0 egress)
    mf = tmp_path / "mpr-test-0001" / "manifest.json"
    assert mf.is_file()
    m = json.loads(mf.read_text(encoding="utf-8"))
    assert m["status"] == "ok" and m["sovereignty_summary"]["violations"] == 0
    assert m["sovereignty_summary"]["offloaded_count"] == 0
    assert all(p["provider_policy"] == "local-only" for p in m["perspectives"])
    assert m["provenance"]["egress"] == [] and m["task_id"] == "KGC-001"
    # memory mirrored exactly once with one distilled entry
    assert len(calls) == 1 and len(calls[0][0]) == 1


# ── MED-1: the manifest records REAL execution, not the planned route ────────────────────────────────
def test_offloadable_domain_manifest_reflects_real_in_engine_execution(tmp_path):
    # competitive is external → offloadable lenses → the plan would offload; the MVP runs every lens
    # in-engine via fanout, so the manifest MUST show in-engine/no-egress (no fictitious offload).
    llm = FakeClassifierLLM(run_panel(domain="competitive", route="wide", mode="comparison",
                                      synthesis_template="comparison-matrix", evidence_source="external"))
    deps = _deps(tmp_path, llm, run_id="mpr-med1")
    out = run_mpr("Wie schlagen wir die etablierten Wettbewerber und wo verlieren wir Marktanteile?",
                  deps=deps)
    assert out.startswith("<<<MPR_REPORT>>>")
    m = json.loads((tmp_path / "mpr-med1" / "manifest.json").read_text(encoding="utf-8"))
    assert all(p["provider_policy"] == "offloadable" for p in m["perspectives"])   # the lenses ARE offloadable
    assert all(p["substrate"] == "in-engine" and p["provider"] == "spark-vllm"     # …yet ran in-engine
               for p in m["perspectives"])
    assert m["provenance"]["egress"] == [] and m["sovereignty_summary"]["offloaded_count"] == 0
    assert m["sovereignty_summary"]["violations"] == 0


# ── MED-4: a fanout length mismatch must not silently truncate the panel ─────────────────────────────
def test_fanout_short_result_is_padded_not_truncated(tmp_path):
    def _short(prompts, *, system, max_tokens, think=True):
        return [{"ok": True, "content": "nur eins", "error": None, "completion_tokens": 1,
                 "latency": 0.1}]        # returns 1 result for a multi-lens panel
    llm = FakeClassifierLLM(run_panel(domain="architecture-decision", route="wide", mode="decision"))
    deps = _deps(tmp_path, llm, run_id="mpr-med4", fanout=_short)
    out = run_mpr("Sollen wir von Modulith auf Microservices umstellen und Optionen vergleichen?",
                  deps=deps)
    assert out.startswith("<<<MPR_REPORT>>>")
    m = json.loads((tmp_path / "mpr-med4" / "manifest.json").read_text(encoding="utf-8"))
    assert len(m["perspectives"]) >= 3                       # full panel kept, not truncated to 1
    failed = [p for p in m["perspectives"] if not p["ok"]]
    assert failed and all(p["error"] == "missing execution result" for p in failed)   # padded lenses marked


# ── MED-3: advisory domain_hint/mode_hint actually reach the classifier ──────────────────────────────
class _CapturingLLM(FakeClassifierLLM):
    def __init__(self, *responses):
        super().__init__(*responses)
        self.last_user = ""

    def complete_json(self, system, user, *, max_tokens, temperature):
        self.last_user = user
        return super().complete_json(system, user, max_tokens=max_tokens, temperature=temperature)


def test_hints_are_threaded_into_the_classifier(tmp_path):
    llm = _CapturingLLM(run_panel(domain="architecture-decision", route="wide", mode="decision"))
    run_mpr("Sollen wir umbauen und die Optionen sorgfältig vergleichen?",
            domain_hint="architecture-decision", mode_hint="decision", deps=_deps(tmp_path, llm))
    assert "domain=architecture-decision" in llm.last_user and "mode=decision" in llm.last_user


# ── panel execution mode (in-engine fanout) — the two switchable best paths ──────────────────────────
def _capturing_fanout(cap):
    def _f(prompts, *, system, max_tokens, think=True):
        cap["think"], cap["max_tokens"] = think, max_tokens
        return [{"ok": True, "content": f"G{i}", "error": None, "completion_tokens": 100, "latency": 0.1}
                for i, _ in enumerate(prompts)]
    return _f


def test_panel_mode_direct_is_thinking_off(tmp_path):
    # DEFAULT "direct" → thinking-OFF + flat budget so each lens writes substantive content (no <think>
    # starvation, the live root cause); full fan-out concurrency.
    cap = {}
    llm = FakeClassifierLLM(run_panel(domain="architecture-decision", route="wide", mode="decision"))
    run_mpr("Sollen wir umbauen und die Optionen abwägen?",
            deps=_deps(tmp_path, llm, fanout=_capturing_fanout(cap), run_id="pm-direct"))
    assert cap["think"] is False and cap["max_tokens"] == 4096


def test_panel_mode_deep_is_thinking_on_with_effort_budget(tmp_path):
    # "deep" → thinking-ON + the panel's per-effort token budget (deeper; the governor throttles concurrency).
    cap = {}
    deps = _deps(tmp_path, FakeClassifierLLM(run_panel(domain="architecture-decision", route="wide",
                 mode="decision")), fanout=_capturing_fanout(cap), run_id="pm-deep")
    deps.panel_mode = "deep"
    run_mpr("Sollen wir umbauen und die Optionen abwägen?", deps=deps)
    assert cap["think"] is True
    assert cap["max_tokens"] in set(EFFORT_MAX_TOKENS.values()) and cap["max_tokens"] >= 4096


def test_invalid_audit_level_falls_back_keeps_artifacts(tmp_path):
    # Live-Bug #5: the model fills the audit_level tool param with "high" (an effort value). An
    # unrecognised value must NOT silently suppress the run artifacts — it falls back to the configured
    # audit_level, so synthesis.md + perspective files (the sovereignty proof) are still written.
    llm = FakeClassifierLLM(run_panel(domain="architecture-decision", route="wide", mode="decision"))
    deps = _deps(tmp_path, llm, run_id="lb5")            # deps.audit_level = "full-per-perspective"
    out = run_mpr("Modulith oder Microservices?", audit_level="high", deps=deps)
    assert out.startswith("<<<MPR_REPORT>>>")
    run_dir = tmp_path / "lb5"
    m = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert m["audit_level"] == "full-per-perspective"   # "high" ignored → configured default
    assert (run_dir / "synthesis.md").is_file()          # artifacts NOT suppressed
    assert (run_dir / "perspective_01.md").is_file()


def test_valid_audit_level_is_honored(tmp_path):
    # a valid model/caller value (manifest-only) is still respected → no per-perspective files (by design).
    llm = FakeClassifierLLM(run_panel(domain="architecture-decision", route="wide", mode="decision"))
    deps = _deps(tmp_path, llm, run_id="lb5b")
    run_mpr("Modulith oder Microservices?", audit_level="manifest-only", deps=deps)
    run_dir = tmp_path / "lb5b"
    m = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert m["audit_level"] == "manifest-only"
    assert not (run_dir / "synthesis.md").is_file()      # honored: manifest-only writes no synthesis.md


def test_synth_llm_binding_disables_thinking(monkeypatch):
    # Live-Bug #4: the synthesis call emits a STRUCTURED decision-matrix JSON, so the real engine binding
    # (_engine_deps) must disable qwen3 thinking — like the classifier + panel "direct". With thinking ON
    # the <think> block starves the budget → extract_json fails → template-parse degrade (the empty CLI
    # result). The stub-Deps tests bypass this binding, so guard the wiring here against a fake _WORKERS.
    import sys
    from types import SimpleNamespace
    cap = {}
    def _fanout(prompts, *, system, max_tokens, think=True):
        cap["think"] = think
        return [{"ok": True, "content": "{}"} for _ in prompts]
    fake_gx10 = SimpleNamespace(
        _EFFECTIVE_CFG=None, _reduce_worker_results=None, _atomic_write=None,
        _store=lambda: None, _DISPATCHER=None,   # engine exposes the lazy accessor, not a _STORE global (#51)
        _WORKERS=SimpleNamespace(client=object(), model="m", fanout=_fanout))
    monkeypatch.setitem(sys.modules, "gx10", fake_gx10)
    from mpr.entry import _engine_deps
    d = _engine_deps()
    assert d.synth_llm is not None                      # binding ran
    d.synth_llm("p", system=None, max_tokens=512)
    assert cap.get("think") is False                    # structured emission → thinking OFF


def test_engine_degrade_formatter_is_bound_and_passed_to_synthesize(monkeypatch, tmp_path):
    import sys
    from types import SimpleNamespace
    import mpr.entry as entry

    formatter = lambda results: "bound"  # noqa: E731
    fake_gx10 = SimpleNamespace(
        _EFFECTIVE_CFG=None, _format_parallel=formatter, _reduce_worker_results=None,
        _atomic_write=None, _store=lambda: None, _DISPATCHER=None, _WORKERS=None)
    monkeypatch.setitem(sys.modules, "gx10", fake_gx10)

    assert entry._engine_deps().degrade_format is formatter

    received = {}
    real_synthesize = entry.synthesize

    def _spy_synthesize(inp, **kwargs):
        received["degrade_format"] = kwargs.get("degrade_format")
        return real_synthesize(inp, **kwargs)

    monkeypatch.setattr(entry, "synthesize", _spy_synthesize)
    llm = FakeClassifierLLM(run_panel(domain="architecture-decision", route="wide", mode="decision"))
    deps = _deps(tmp_path, llm, run_id="mpr-degrade-format")
    deps.degrade_format = formatter

    out = run_mpr("Should we move from a monolith to microservices?", deps=deps)

    assert out.startswith("<<<MPR_REPORT>>>")
    assert received["degrade_format"] is formatter


def test_resolve_store_calls_lazy_accessor():
    # #51: the engine exposes the shared TaskStore via the lazy accessor _store() (gx10.py:1965), NOT a
    # _STORE global. The binding must CALL it (binding the function object made index_in_taskstore no-op →
    # task_id stayed None). This drives the real seam the stub-injecting pipeline tests bypass.
    from types import SimpleNamespace
    from mpr.entry import _resolve_store

    class _Store:
        def create(self, fields, *, force=False, now_iso=None):
            return {**fields, "id": "KGC-001"}

    inst = _Store()
    bound = _resolve_store(SimpleNamespace(_store=lambda: inst))
    assert bound is inst and hasattr(bound, "create")    # the INSTANCE, not the function object
    assert _resolve_store(SimpleNamespace(STORE=inst)) is inst   # fallback to the global when no accessor
    assert _resolve_store(object()) is None               # minimal/old engine stub → None (fail-soft)

    def _boom():
        raise RuntimeError("store down")
    assert _resolve_store(SimpleNamespace(_store=_boom)) is None  # build fault degrades, never raises


def test_full_pipeline_indexes_taskstore_and_backfills_task_id(tmp_path):
    # #51 end-to-end: with a real (stub) store bound, the run is registered and the manifest carries the id.
    store = _FakeStore()
    llm = FakeClassifierLLM(run_panel(domain="architecture-decision", route="wide", mode="decision"))
    deps = _deps(tmp_path, llm, store=store, run_id="mpr-idx-0001")
    out = run_mpr("Sollen wir von Modulith auf Microservices umstellen?", deps=deps)
    assert out.startswith("<<<MPR_REPORT>>>")
    m = json.loads((tmp_path / "mpr-idx-0001" / "manifest.json").read_text(encoding="utf-8"))
    assert m["task_id"] == "KGC-001" and store.created       # indexed + id backfilled (not None)


def test_engine_deps_index_runs_always_on(monkeypatch):
    # #984: MPR is embedded (no reasoning-only project type) — an embedded run's manifest is always
    # indexed in the active (software) initiative's TaskStore. Drives the real _engine_deps seam.
    import sys
    from types import SimpleNamespace
    from mpr.entry import _engine_deps
    base = dict(_EFFECTIVE_CFG=None, _reduce_worker_results=None, _atomic_write=None,
                _store=lambda: None, _DISPATCHER=None, _WORKERS=None)
    monkeypatch.setitem(sys.modules, "gx10", SimpleNamespace(**base))
    assert _engine_deps().index_runs is True


def test_run_skips_taskstore_index_when_index_runs_off(tmp_path):
    # Defensive: the writer honours index_runs=False (manifest written, NO TaskStore entry, task_id None).
    # #984: production always sets index_runs=True, but the branch stays guarded.
    store = _FakeStore()
    llm = FakeClassifierLLM(run_panel(domain="architecture-decision", route="wide", mode="decision"))
    deps = _deps(tmp_path, llm, store=store, run_id="mpr-noidx")
    deps.index_runs = False
    out = run_mpr("Should we move from a monolith to microservices?", deps=deps)
    assert out.startswith("<<<MPR_REPORT>>>")
    m = json.loads((tmp_path / "mpr-noidx" / "manifest.json").read_text(encoding="utf-8"))
    assert m["task_id"] is None and store.created == []      # no TaskStore entry when indexing is off
