"""Always-on deterministic staging verification and advisory grounding (#1466 F5a)."""
from __future__ import annotations

import builtins
import json
import sys
import types
from pathlib import Path

import pytest

from design_test_support import approve_active_design

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=lambda **kw: object()))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402
from ack import hooks  # noqa: E402


class _FakeMem:
    def __init__(self, hits):
        self._hits = list(hits)
        self.searched = []

    def is_available(self):
        return True

    def search(self, query, limit=5):
        self.searched.append(query)
        return list(self._hits)


class _RaisingMem:
    def is_available(self):
        raise RuntimeError("memory down")


class _PartialMem:
    def is_available(self):
        return True

    def search(self, query, limit=5):
        return ["stored validation evidence"] if "validation checks" in query else []


_GOOD_TASK = ('{"type":"feature","priority":"high","title":"Build the order service",'
              '"description":"Implement the full order service with validation, persistence and tests."}')
_THIN_TASK = '{"type":"feature","priority":"high","title":"Fix","description":"do it"}'
_HANDOVER = "## Handover\nbuild the order service end to end with full validation and tests"
_PARTIAL_HANDOVER = """## Handover
Implement the order-service validation checks with complete tests.
Persist orders through the approved relational schema and migrations.
Expose latency and failure metrics through the service telemetry endpoint.
"""


@pytest.fixture(autouse=True)
def _reset():
    hooks.clear_hooks()
    gx10._set_last_verdict(None)
    gx10._QUALITY_TRIPPED = None
    gx10._QUALITY_BREAKER = None
    yield
    hooks.clear_hooks()
    gx10._set_last_verdict(None)
    gx10._QUALITY_TRIPPED = None
    gx10._QUALITY_BREAKER = None
    gx10._apply_config(gx10._code_defaults())


def _prepare(tmp_path, monkeypatch, *, mem=None, cfg=None):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(gx10, "_MEMORY", mem)
    gx10.STORE = None
    gx10._apply_config(cfg or gx10._code_defaults())
    gx10._dispatch(types.SimpleNamespace(run=lambda t: None, save_session=lambda: None, status=lambda: "ok"),
                   "initiative new Order Service --type software")
    approve_active_design(gx10)


def _stage_impl(task_json=_GOOD_TASK, handover=_HANDOVER):
    return gx10._stage_handover_impl(None, "OPUS", handover, task_json=task_json, force=True)


def test_required_rules_and_grounding_produce_combined_verdict(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch, mem=_FakeMem(["a prior order-service memory"]))
    out = _stage_impl()
    assert out.startswith("OK")
    verdict = gx10._last_verdict()
    assert verdict is not None and verdict.verifier == "handover"
    assert verdict.passed and verdict.score == 1.0
    assert hooks.hook_count("pre_handover") == 0


def test_grounding_failure_is_advisory_not_a_staging_gate(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch, mem=_FakeMem([]))
    out = _stage_impl()
    assert out.startswith("OK")
    verdict = gx10._last_verdict()
    assert verdict is not None and verdict.passed and verdict.score == 1.0
    assert "grounding unavailable" in verdict.reason and "no grounding hits" in verdict.reason


def test_grounding_hits_that_do_not_cover_all_claims_are_real_degradation(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch, mem=_PartialMem())
    out = _stage_impl(handover=_PARTIAL_HANDOVER)
    assert out.startswith("OK")
    verdict = gx10._last_verdict()
    assert verdict is not None and not verdict.passed
    assert verdict.score == pytest.approx(2 / 3)
    assert "grounding 0.33" in verdict.reason and "unavailable" not in verdict.reason


@pytest.mark.parametrize("mem, marker", [(None, "no memory tier"), (_RaisingMem(), "memory error")])
def test_grounding_unavailable_is_reported_separately_from_rules_pass(tmp_path, monkeypatch, mem, marker):
    _prepare(tmp_path, monkeypatch, mem=mem)
    fields = json.loads(_GOOD_TASK)
    assert gx10._required_verifier_gate(fields) is None
    gx10._record_advisory_grounding(fields, _HANDOVER)
    verdict = gx10._last_verdict()
    assert verdict is not None and verdict.passed and verdict.score == 1.0
    assert "grounding unavailable" in verdict.reason and marker in verdict.reason


def test_required_rule_failure_refuses_before_any_create_or_handover_write(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)
    store = gx10._store()
    create_calls = []
    real_create = store.create
    monkeypatch.setattr(store, "create", lambda *a, **k: (create_calls.append(1), real_create(*a, **k))[1])

    out = gx10._stage_handover(None, "OPUS", _HANDOVER, task_json=_THIN_TASK, force=True)

    assert "description_substantive" in out and "title_specific" in out
    assert create_calls == [] and store.list("pending") == []
    assert list(gx10.handovers_dir().glob("*.md")) == []


def test_required_rules_gate_create_not_rehand(tmp_path, monkeypatch):
    """The required verifier rules validate a NEWLY AUTHORED task_json (the create path). A pure re-hand of an
    already-created task is NOT re-blocked by the stored task's field quality — the task was authored + validated
    at create, and re-checking it would deadlock tasks created by plan_units decomposition (terse titles, no new
    task_json). Ambiguity + the quality hold still gate the re-hand write."""
    _prepare(tmp_path, monkeypatch, mem=_FakeMem(["grounded"]))
    assert gx10._stage_handover(None, "OPUS", _HANDOVER, task_json=_GOOD_TASK, force=True).startswith("OK")
    store = gx10._store()
    existing = store.list("pending")[0]
    real_get = store.get

    def thin_get(task_id):
        task = real_get(task_id)
        return {**task, "title": "Fix", "description": "do it"} if task else None

    monkeypatch.setattr(store, "get", thin_get)
    out = gx10._stage_handover(existing["id"], "OPUS", "## Handover\ncontinue the validated implementation")
    assert out.startswith("OK")                                      # re-hand not blocked by stored terse fields
    assert "description_substantive" not in out and "title_specific" not in out


@pytest.mark.parametrize("kind", ["stalled", "errored"])
def test_blocked_runtime_annotations_do_not_deadlock_pure_rehand(tmp_path, monkeypatch, kind):
    _prepare(tmp_path, monkeypatch, mem=_FakeMem(["grounded"]))
    assert gx10._stage_handover(None, "OPUS", _HANDOVER, task_json=_GOOD_TASK, force=True).startswith("OK")
    store = gx10._store()
    tid = store.list("pending")[0]["id"]
    assert gx10._stage_handover(tid, "OPUS", "## Handover\nprepare the recovery handover").startswith("OK")
    store.mark_blocked(tid, reason=f"{kind} recovery required", kind=kind)
    task_path, _ = store._find(tid)
    assert task_path is not None
    before_task = task_path.read_bytes()

    out = gx10._stage_handover(tid, "OPUS", f"## Handover\nresume the {kind} task with recovery checks")

    assert out.startswith("OK") and "violates the ACK contract" not in out
    assert "handover written:" in out
    assert f"resume the {kind} task with recovery checks" in (
        gx10.handovers_dir() / f"{tid}_OPUS.md"
    ).read_text(encoding="utf-8")
    assert task_path.read_bytes() == before_task


def test_required_verifier_import_failure_refuses_before_write(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)
    real_import = builtins.__import__

    def unavailable(name, *args, **kwargs):
        if name == "ack.verify":
            raise ImportError("verifier missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", unavailable)
    out = gx10._stage_handover(None, "OPUS", _HANDOVER, task_json=_GOOD_TASK, force=True)
    assert "required verifier unavailable" in out and "fail-closed" in out
    assert gx10._store().list("pending") == []
    assert list(gx10.handovers_dir().glob("*.md")) == []


@pytest.mark.parametrize("legacy", [True, False], ids=["legacy-true", "legacy-false"])
def test_verify_enabled_tombstone_cannot_disable_required_rules(tmp_path, monkeypatch, capsys, legacy):
    cfg = gx10._code_defaults()
    assert "enabled" not in cfg["verify"]
    cfg["verify"]["enabled"] = legacy
    _prepare(tmp_path, monkeypatch, cfg=cfg)

    warnings = [line for line in capsys.readouterr().out.splitlines() if "DEPRECATED" in line]
    assert len(warnings) == 1 and "verify.enabled" in warnings[0] and "always on" in warnings[0]
    assert "enabled" not in cfg["verify"]
    assert "required handover verifier" in _stage_impl(_THIN_TASK)


def test_runtime_set_refuses_retired_verify_switch(monkeypatch):
    cfg = gx10._code_defaults()
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", cfg)
    surfaced = []
    monkeypatch.setattr(gx10, "_ui_print", lambda message, *a, **k: surfaced.append(str(message)))
    gx10._dispatch(None, "config set verify.enabled false")
    assert len(surfaced) == 1 and "retired and cannot be set" in surfaced[0]
