"""Variant-B ambiguity detection and its always-on no-guessing staging gate (#1466 F5a)."""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from design_test_support import approve_active_design

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=object))

_CORE = Path(__file__).resolve().parents[2]
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))
if str(_CORE / "engine") not in sys.path:
    sys.path.insert(0, str(_CORE / "engine"))       # gx10 lives under core/engine

from ack.ace.fork import ambiguity_signals, detect_ambiguity  # noqa: E402

import gx10  # noqa: E402


_GOOD_TASK = ('{"type":"feature","priority":"high","title":"Build the order service",'
              '"description":"Implement the full order service with validation, persistence and tests."}')


@pytest.fixture(autouse=True)
def _reset():
    gx10._QUALITY_TRIPPED = None
    gx10._QUALITY_BREAKER = None
    yield
    gx10._QUALITY_TRIPPED = None
    gx10._QUALITY_BREAKER = None


def _prepare(tmp_path, monkeypatch, cfg=None):
    monkeypatch.chdir(tmp_path)
    gx10.STORE = None
    gx10._apply_config(cfg or gx10._code_defaults())
    gx10._dispatch(types.SimpleNamespace(run=lambda t: None, save_session=lambda: None, status=lambda: "ok"),
                   "initiative new Order Service --type software")
    approve_active_design(gx10)


def test_flags_uncertainty_markers():
    for txt in ["The API should return TBD", "handle it somehow", "not sure if we need auth", "figure out the format"]:
        assert ambiguity_signals(txt), txt
        assert detect_ambiguity(txt) is not None


def test_flags_open_question_and_multiple_interpretations():
    assert detect_ambiguity("Should the cache expire after 5m or 10m?") is not None      # '?'
    assert detect_ambiguity("Store it in either Redis or Postgres") is not None          # either/or
    assert detect_ambiguity("Retry the request as appropriate") is not None              # vague qualifier


def test_clear_requirements_are_not_flagged():
    for txt in ["Add a GET /metrics endpoint returning latency p50 and p95 as JSON.",
                "Cap docker json-file logs at 10MB with 3 rotations.",
                "Write the file to state_root/audit/ledger.jsonl."]:
        assert ambiguity_signals(txt) == [], txt
        assert detect_ambiguity(txt) is None


def test_forksignal_shape_and_fields():
    sig = detect_ambiguity("just do it somehow", unit="KGC-9", area="requirements")
    assert sig is not None and not sig.is_empty()
    assert sig.unit == "KGC-9" and sig.area == "requirements" and sig.question and sig.options


def test_empty_text_is_unambiguous():
    assert detect_ambiguity("") is None and ambiguity_signals("") == []
    assert ambiguity_signals(None) == []                                                 # type: ignore[arg-type]


def test_ambiguity_gate_refusal_contains_question_and_options():
    out = gx10._ambiguity_gate("just do it somehow", "T1")
    assert out and "Ambiguity detected" in out and "Options:" in out
    assert "Ask the operator" in out and "explicitly stated assumption" in out
    assert gx10._ambiguity_gate(
        "Add a GET /metrics endpoint returning p50/p95 latency as JSON.", "T2") is None


def test_ambiguity_hit_refuses_before_create_and_handover_write(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)
    store = gx10._store()
    create_calls = []
    real_create = store.create
    monkeypatch.setattr(store, "create", lambda *a, **k: (create_calls.append(1), real_create(*a, **k))[1])

    out = gx10._stage_handover(
        None, "OPUS", "## Handover\nImplement the order service somehow", task_json=_GOOD_TASK, force=True)

    assert "ambiguous handover refused" in out and "Options:" in out
    assert create_calls == [] and store.list("pending") == []
    assert list(gx10.handovers_dir().glob("*.md")) == []


def test_ambiguity_gate_also_holds_rehand_before_handover_write(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)
    clear = "## Handover\nImplement the complete validated order service"
    assert gx10._stage_handover(None, "OPUS", clear, task_json=_GOOD_TASK, force=True).startswith("OK")
    existing = gx10._store().list("pending")[0]
    handover = gx10.handovers_dir() / f"{existing['id']}_OPUS.md"
    before = handover.read_bytes()

    out = gx10._stage_handover(existing["id"], "OPUS", "## Handover\ncontinue it somehow")

    assert "ambiguous handover refused" in out and "Options:" in out
    assert handover.read_bytes() == before


def test_detector_internal_error_refuses_before_create_and_handover_write(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)
    import ack.ace.fork as fork
    monkeypatch.setattr(fork, "ambiguity_signals", lambda text: (_ for _ in ()).throw(RuntimeError("boom")))
    store = gx10._store()
    create_calls = []
    real_create = store.create
    monkeypatch.setattr(store, "create", lambda *a, **k: (create_calls.append(1), real_create(*a, **k))[1])

    out = gx10._stage_handover(
        None, "OPUS", "## Handover\nImplement the complete validated order service",
        task_json=_GOOD_TASK, force=True)

    assert "detector unavailable" in out and "fail-closed" in out
    assert create_calls == [] and store.list("pending") == []
    assert list(gx10.handovers_dir().glob("*.md")) == []


@pytest.mark.parametrize("legacy", [True, False], ids=["legacy-true", "legacy-false"])
def test_ambiguity_config_tombstone_cannot_disable_gate(tmp_path, monkeypatch, capsys, legacy):
    cfg = gx10._code_defaults()
    assert "ambiguity_detect" not in cfg.get("safety", {})
    cfg["safety"]["ambiguity_detect"] = legacy
    _prepare(tmp_path, monkeypatch, cfg)

    warnings = [line for line in capsys.readouterr().out.splitlines() if "DEPRECATED" in line]
    assert len(warnings) == 1 and "safety.ambiguity_detect" in warnings[0] and "always on" in warnings[0]
    assert "ambiguity_detect" not in cfg.get("safety", {})
    out = gx10._stage_handover(
        None, "OPUS", "## Handover\nImplement the order service somehow", task_json=_GOOD_TASK, force=True)
    assert "ambiguous handover refused" in out


def test_retired_ambiguity_env_warns_and_is_ignored(monkeypatch, capsys):
    cfg = gx10._code_defaults()
    monkeypatch.setenv("GX10_AMBIGUITY_DETECT", "0")
    gx10._apply_env(cfg)
    assert "GX10_AMBIGUITY_DETECT" in capsys.readouterr().out
    assert "ambiguity_detect" not in cfg["safety"]


def test_runtime_set_refuses_retired_ambiguity_switch(monkeypatch):
    cfg = gx10._code_defaults()
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", cfg)
    surfaced = []
    monkeypatch.setattr(gx10, "_ui_print", lambda message, *a, **k: surfaced.append(str(message)))
    gx10._dispatch(None, "config set safety.ambiguity_detect false")
    assert len(surfaced) == 1 and "retired and cannot be set" in surfaced[0]


def test_fork_mpr_off_cannot_disable_always_on_ambiguity_refusal(tmp_path, monkeypatch):
    cfg = gx10._code_defaults()
    cfg["ace"]["fork_mpr"] = {"enabled": False}
    _prepare(tmp_path, monkeypatch, cfg)
    assert gx10._ACE_FORK_MPR is False

    out = gx10._stage_handover(
        None, "OPUS", "## Handover\nImplement the order service somehow", task_json=_GOOD_TASK, force=True)

    assert "ambiguous handover refused" in out
    assert gx10._store().list("pending") == []
    assert list(gx10.handovers_dir().glob("*.md")) == []
