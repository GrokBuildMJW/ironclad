"""Always-on verifier-to-quality wiring and the synchronous staging hold (#1466 F5a)."""
from __future__ import annotations

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
from ack.verify import VerdictResult  # noqa: E402


class _FakeMem:
    def __init__(self, hits):
        self._hits = list(hits)

    def is_available(self):
        return True

    def search(self, query, limit=5):
        return list(self._hits)


_GOOD = ('{"type":"feature","priority":"high","title":"Build the order service",'
         '"description":"Implement the full order service with validation, persistence and tests."}')
_SECOND = ('{"type":"feature","priority":"high","title":"Build the payment service",'
           '"description":"Implement the complete payment service with validation, persistence and tests."}')
_THIRD = ('{"type":"feature","priority":"high","title":"Build the inventory service",'
          '"description":"Implement the complete inventory service with validation, persistence and tests."}')
_HANDOVER = "## Handover\nbuild the order service end to end with full validation and tests"


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


def _prepare(tmp_path, monkeypatch, *, hits, threshold=0.5, min_consecutive=1, cfg=None):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(gx10, "_MEMORY", _FakeMem(hits))
    effective = cfg or gx10._code_defaults()
    effective["quality"]["threshold"] = threshold
    effective["quality"]["min_consecutive"] = min_consecutive
    gx10.STORE = None
    gx10._apply_config(effective)
    gx10._dispatch(types.SimpleNamespace(run=lambda t: None, save_session=lambda: None, status=lambda: "ok"),
                   "initiative new Order Service --type software")
    approve_active_design(gx10)
    return effective


def test_empty_memory_does_not_false_trip_repeated_good_creates(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch, hits=[], threshold=0.75)
    for task_json in (_GOOD, _SECOND, _THIRD):
        out = gx10._stage_handover(None, "OPUS", _HANDOVER, task_json=task_json, force=True)
        assert out.startswith("OK")
        assert gx10._quality_tripped() is None
    assert gx10._quality_breaker().snapshot().samples == 3
    assert len(gx10._store().list("pending")) == 3
    assert hooks.hook_count("post_handover") == 1


def test_good_combined_verdict_does_not_trip(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch, hits=["grounded"], threshold=0.75)
    out = gx10._stage_handover(None, "OPUS", _HANDOVER, task_json=_GOOD, force=True)
    assert out.startswith("OK")
    assert gx10._quality_tripped() is None
    assert gx10._quality_breaker().snapshot().samples == 1


def test_latched_quality_trip_holds_create_and_rehand_before_writes(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch, hits=[], threshold=0.75)
    first = gx10._stage_handover(None, "OPUS", _HANDOVER, task_json=_GOOD, force=True)
    assert first.startswith("OK")
    store = gx10._store()
    tid = store.list("pending")[0]["id"]
    handover = gx10.handovers_dir() / f"{tid}_OPUS.md"
    before_files = {path: path.read_bytes() for path in gx10.handovers_dir().glob("*.md")}
    create_calls = []
    real_create = store.create
    monkeypatch.setattr(store, "create", lambda *a, **k: (create_calls.append(1), real_create(*a, **k))[1])
    gx10._quality_breaker().record(0.0)
    gx10._QUALITY_TRIPPED = gx10._quality_breaker().snapshot()
    events = []
    real_emit = gx10._emit_hook

    def capture(event, ctx=None):
        events.append((event, ctx))
        return real_emit(event, ctx)

    monkeypatch.setattr(gx10, "_emit_hook", capture)

    rehand_out = gx10._stage_handover(tid, "OPUS", "## Handover\ncontinue the validated order service work")
    create_out = gx10._stage_handover(None, "OPUS", _HANDOVER, task_json=_SECOND, force=True)

    assert "output-quality breaker tripped" in create_out
    assert "output-quality breaker tripped" in rehand_out
    assert "/quality reset" in create_out and "/quality reset" in rehand_out
    assert create_calls == [] and len(store.list("pending")) == 1
    assert {path: path.read_bytes() for path in gx10.handovers_dir().glob("*.md")} == before_files
    assert handover.read_bytes() == before_files[handover]
    escalations = [ctx for event, ctx in events if event == "escalation"]
    assert len(escalations) == 2 and all(ctx["kind"] == "output_quality" for ctx in escalations)


def test_passing_quality_score_clears_latched_staging_hold(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch, hits=[], threshold=0.75, min_consecutive=3)

    for _ in range(3):
        gx10._set_last_verdict(VerdictResult(False, 0.5, "low quality", "handover"))
        gx10._quality_consumer_hook({})
    assert gx10._quality_tripped() is not None

    held = gx10._stage_handover(None, "OPUS", _HANDOVER, task_json=_GOOD, force=True)
    assert "passing-quality submission clears the breaker" in held
    assert gx10._store().list("pending") == []
    assert list(gx10.handovers_dir().glob("*.md")) == []
    assert gx10._quality_tripped() is None
    assert not gx10._quality_breaker().tripped

    out = gx10._stage_handover(None, "OPUS", _HANDOVER, task_json=_GOOD, force=True)
    assert out.startswith("OK")
    assert len(gx10._store().list("pending")) == 1
    assert len(list(gx10.handovers_dir().glob("*.md"))) == 1


def test_operator_quality_reset_clears_trip_and_next_create_proceeds(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch, hits=["grounded"], threshold=0.75, min_consecutive=1)
    gx10._set_last_verdict(VerdictResult(False, 0.0, "low quality", "handover"))
    gx10._quality_consumer_hook({})
    assert gx10._quality_tripped() is not None and gx10._quality_breaker().tripped

    surfaced = []
    monkeypatch.setattr(gx10, "_ui_print", lambda message, *a, **k: surfaced.append(str(message)))
    gx10._dispatch(None, "quality reset")

    assert gx10._quality_tripped() is None and not gx10._quality_breaker().tripped
    assert len(surfaced) == 1 and "staging hold cleared" in surfaced[0]
    out = gx10._stage_handover(None, "OPUS", _HANDOVER, task_json=_GOOD, force=True)
    assert out.startswith("OK")
    assert len(gx10._store().list("pending")) == 1


@pytest.mark.parametrize("legacy", [True, False], ids=["legacy-true", "legacy-false"])
def test_quality_enabled_tombstone_cannot_disable_breaker_or_consumer(
        tmp_path, monkeypatch, capsys, legacy):
    cfg = gx10._code_defaults()
    assert "enabled" not in cfg["quality"]
    cfg["quality"]["enabled"] = legacy
    _prepare(tmp_path, monkeypatch, hits=["grounded"], cfg=cfg)

    warnings = [line for line in capsys.readouterr().out.splitlines() if "DEPRECATED" in line]
    assert len(warnings) == 1 and "quality.enabled" in warnings[0] and "always on" in warnings[0]
    assert "enabled" not in cfg["quality"]
    assert gx10._quality_breaker() is not None and hooks.hook_count("post_handover") == 1


def test_runtime_set_refuses_retired_quality_switch(monkeypatch):
    cfg = gx10._code_defaults()
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", cfg)
    surfaced = []
    monkeypatch.setattr(gx10, "_ui_print", lambda message, *a, **k: surfaced.append(str(message)))
    gx10._dispatch(None, "config set quality.enabled false")
    assert len(surfaced) == 1 and "retired and cannot be set" in surfaced[0]
