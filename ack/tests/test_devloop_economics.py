"""Loop economics + autopilot reconciliation (epic #262, S15 / ADR 0002), offline.

Lives in `scripts/devloop/` (private) -> skips in an installed/clean-room tree. Pins the poison-task
cap, deterministic backoff, transient-vs-hard retry classification, the cost ceiling, and the
single-steering-authority reconciliation (driver + autopilot enabled => violation).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_ECON = _REPO / "scripts" / "devloop" / "economics.py"

pytestmark = pytest.mark.skipif(
    not _ECON.is_file(),
    reason="private dev-loop economics (scripts/devloop/economics.py) absent — clean-room tree",
)


def _load():
    spec = importlib.util.spec_from_file_location("_devloop_economics", _ECON)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_poison_task_cap_and_deterministic_backoff():
    e = _load()
    assert not e.exhausted(2, cap=3) and e.exhausted(3, cap=3)
    b = [e.backoff_seconds(i, base=1.0, factor=2.0, ceiling=10.0) for i in range(6)]
    assert b[0] == 1.0 and b == sorted(b) and b[-1] == 10.0          # monotonic, capped
    assert e.backoff_seconds(0) == e.backoff_seconds(0)              # deterministic


def test_retry_only_transient_failures():
    e = _load()
    assert e.should_retry("429 Too Many Requests")
    assert e.should_retry("Anthropic API overloaded")
    assert e.should_retry("read timeout")
    assert not e.should_retry("404 not found")
    assert not e.should_retry("invalid api key")


def test_cost_ceiling():
    e = _load()
    assert e.within_cost_budget(50, 100) and not e.within_cost_budget(100, 100)
    assert e.within_cost_budget(1e9, None)                          # None => unbounded


def test_autopilot_conflict_single_steering_authority():
    e = _load()
    assert e.autopilot_conflict(driver_active=True, autopilot_enabled=False) == []
    assert e.autopilot_conflict(driver_active=False, autopilot_enabled=True) == []
    conflict = e.autopilot_conflict(driver_active=True, autopilot_enabled=True)
    assert conflict and "single authority" in conflict[0]


def test_should_abort_on_poison_cap_or_over_budget():
    e = _load()
    assert e.should_abort(attempt=0, cap=3) == []                                  # fresh, no ceiling => keep going
    assert any("poison-cap" in r for r in e.should_abort(attempt=3, cap=3))         # cap reached => ABORT
    assert any("cost ceiling" in r for r in e.should_abort(attempt=0, cap=3, spent=5.0, ceiling=1.0))
    assert e.should_abort(attempt=1, cap=3, spent=0.5, ceiling=1.0) == []           # under both => keep going


def test_delivery_stage_poison_cap_is_distinct_and_lower(monkeypatch):
    # #362 S12: the delivery STAGE cap is separate from the agent-attempt cap and gives up sooner
    # (each retry re-fires heavy paid build/publish/smoke steps).
    e = _load()
    assert e.DELIVERY_STAGE_CAP < 3                                                 # lower than a typical agent cap
    assert e.delivery_stage_should_abort(attempt=0) == []                           # fresh => a retry is permitted
    aborts = e.delivery_stage_should_abort(attempt=e.DELIVERY_STAGE_CAP)
    assert any("delivery-stage poison-cap" in r for r in aborts)                    # cap reached => ABORT the stage
    assert any("delivery-stage cost" in r
               for r in e.delivery_stage_should_abort(attempt=0, spent=5.0, ceiling=1.0))


def test_review_cost_entry_normalizes_and_rejects_bad_type():
    # #493 P6: a per-iteration review-cost record, normalized; an unknown change_type must raise
    # so a typo can't silently skew the by-type summary.
    e = _load()
    rec = e.review_cost_entry(497, "Doc", rounds=1, agents=["claude"], approx_tokens=1200)
    assert rec["change_type"] == "doc" and rec["rounds"] == 1 and rec["issue"] == 497
    assert rec["agents"] == ["claude"] and rec["approx_tokens"] == 1200 and rec["efforts"] == []
    assert e.review_cost_entry(1, "code", rounds=-5, agents=[])["rounds"] == 0   # clamped non-negative
    with pytest.raises(ValueError):
        e.review_cost_entry(1, "essay", rounds=1, agents=[])                     # unknown type => raise


def test_summarize_review_cost_breaks_down_by_change_type():
    # The tiering (#493 P1) should show: doc iterations cost fewer rounds than code.
    e = _load()
    entries = [
        e.review_cost_entry(1, "doc", rounds=1, agents=["claude"], approx_tokens=500),
        e.review_cost_entry(2, "code", rounds=3, agents=["claude", "codex"], approx_tokens=8000),
        e.review_cost_entry(3, "doc", rounds=1, agents=["claude"], approx_tokens=400),
    ]
    s = e.summarize_review_cost(entries)
    assert s["iterations"] == 3 and s["total_rounds"] == 5 and s["total_approx_tokens"] == 8900
    assert s["by_change_type"]["doc"] == {"iterations": 2, "rounds": 2, "approx_tokens": 900}
    assert s["by_change_type"]["code"]["rounds"] == 3
    assert e.summarize_review_cost([]) == {"iterations": 0, "total_rounds": 0,
                                           "total_approx_tokens": 0, "by_change_type": {}}
