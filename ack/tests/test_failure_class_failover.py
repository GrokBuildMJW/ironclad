"""#602 2.4 / #805 — FailureClass produced at the code-agent failover (for the Strategy consumer 2.5).

`gx10._record_failure_class(result)` maps a code-agent run result onto the shared `FailureClass` and records
it (`gx10._last_failure_class()`) so the Strategy Revisor (#806) can act on WHY a run failed. OPT-IN per
`strategy.enabled` (default OFF → None recorded, byte-identical). The server's /feedback path calls it and
surfaces the class in the response; here we test the engine recorder directly.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=lambda **kw: object()))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10
import providers
from ack.failure_class import FailureClass


@pytest.fixture(autouse=True)
def _reset():
    gx10._LAST_FAILURE_CLASS = None
    yield
    gx10._LAST_FAILURE_CLASS = None
    gx10._apply_config(gx10._code_defaults())   # restore defaults → strategy off


def _enable(on):
    cfg = gx10._code_defaults()
    if on:
        cfg["strategy"]["enabled"] = True
    gx10._apply_config(cfg)


def test_unavailable_records_failure_class_when_enabled():
    _enable(True)
    assert gx10._record_failure_class(providers.RESULT_UNAVAILABLE) == FailureClass.UNAVAILABLE.value
    assert gx10._last_failure_class() == FailureClass.UNAVAILABLE


def test_failed_maps_to_incomplete_output():
    _enable(True)
    assert gx10._record_failure_class(providers.RESULT_FAILED) == FailureClass.INCOMPLETE_OUTPUT.value
    assert gx10._last_failure_class() == FailureClass.INCOMPLETE_OUTPUT


def test_ok_result_is_not_a_failure():
    _enable(True)
    assert gx10._record_failure_class(providers.RESULT_OK) is None
    assert gx10._last_failure_class() is None        # a successful run never records a failure class


def test_default_off_is_byte_identical_no_record():
    _enable(False)
    assert gx10._record_failure_class(providers.RESULT_UNAVAILABLE) is None
    assert gx10._last_failure_class() is None         # opt-in: nothing recorded when strategy.enabled is off
