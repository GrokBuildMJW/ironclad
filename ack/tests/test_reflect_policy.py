"""#503 / #767 (MEMSVC-1) — the pure reflection-trigger policy.

Proves the threshold-fire decision consumes the counter at fire time and suppresses a fire while a reflection
is already running, so writes during a (slow) run accumulate toward the next cycle (no undercount) and no
bail-thread is spawned (no churn). Pure + offline (no mem0/neo4j/fastapi).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

# Load the pure policy module DIRECTLY by path — do NOT put memory-service/ on sys.path: it holds an
# ``app.py`` that would shadow the engine's ``app`` module for the rest of the pytest session.
_RP_PATH = Path(__file__).resolve().parents[2] / "memory-service" / "reflect_policy.py"
_spec = importlib.util.spec_from_file_location("reflect_policy", _RP_PATH)
reflect_policy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reflect_policy)
reflect_decision = reflect_policy.reflect_decision


def test_increments_below_threshold_no_fire():
    assert reflect_decision(0, 50, False) == (1, False)
    assert reflect_decision(10, 50, False) == (11, False)
    assert reflect_decision(48, 50, False) == (49, False)


def test_fires_and_consumes_at_threshold_when_idle():
    # the 50th write (49 -> 50) reaches the threshold + nothing running -> fire AND reset to 0
    assert reflect_decision(49, 50, False) == (0, True)


def test_no_fire_while_running_counter_accumulates():
    # a run is in progress -> never fire; the counter keeps climbing (no undercount of during-run writes)
    assert reflect_decision(49, 50, True) == (50, False)
    assert reflect_decision(50, 50, True) == (51, False)
    assert reflect_decision(120, 50, True) == (121, False)


def test_fires_on_backlog_once_the_run_finished():
    # run done (not running) + counter already past the threshold -> fire on the accumulated backlog
    assert reflect_decision(120, 50, False) == (0, True)


def test_disabled_when_threshold_non_positive_or_bad():
    # every < 1 (or non-int) disables firing — the counter just accumulates, never raises
    assert reflect_decision(100, 0, False) == (101, False)
    assert reflect_decision(100, -5, False) == (101, False)
    assert reflect_decision(100, "x", False) == (101, False)
    assert reflect_decision(5, float("inf"), False) == (6, False)   # int(inf) -> OverflowError -> disabled


def test_never_raises_on_garbage_counter():
    assert reflect_decision(None, 50, False) == (1, False)
    assert reflect_decision("nope", 50, False) == (1, False)
    assert reflect_decision(float("inf"), 50, False) == (1, False)  # int(inf) -> OverflowError -> counter resets to 1
