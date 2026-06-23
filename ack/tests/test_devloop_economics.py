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
