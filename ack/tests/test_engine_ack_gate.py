"""Engine ↔ ACK wiring: the stage_handover soft-path validation gate (_ack_validate).

Imports the orchestration engine (core/engine/gx10.py) with a stubbed ``openai`` so
it loads without the heavy dependency, then exercises the gate directly. The gate is
the integration seam: a model-emitted task_json is validated against the ACK contract
before the TaskStore mutates anything; on a violation the exact error is returned so
the agent loop re-asks.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

# Stub the heavy optional dep so the engine module imports (it sys.exits on a real
# ImportError of openai). setdefault keeps a real install if present.
sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=object))

# core/engine on sys.path so `import gx10` works (core/ is already added by conftest).
_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402
import pytest  # noqa: E402

VALID = {"type": "implementation", "priority": "high", "title": "t", "description": "d"}


@pytest.fixture(autouse=True)
def _restore_flags():
    """Save/restore the engine's ACK flags around each test (module globals)."""
    ack, lode = gx10.ACK_ENABLED, gx10.LODESTAR_ENABLED
    yield
    gx10.ACK_ENABLED, gx10.LODESTAR_ENABLED = ack, lode


def test_gate_on_valid_passes():
    gx10.ACK_ENABLED = True
    assert gx10._ack_validate(VALID) is None


def test_gate_missing_required_field_errors():
    gx10.ACK_ENABLED = True
    err = gx10._ack_validate({"type": "implementation", "priority": "high", "title": "t"})
    assert err and "description" in err


def test_gate_rejects_extra_key():
    gx10.ACK_ENABLED = True
    assert gx10._ack_validate({**VALID, "bogus": "x"})  # extra='forbid'


def test_lodestar_requires_capability_for_buildable():
    gx10.ACK_ENABLED = True
    gx10.LODESTAR_ENABLED = True
    err = gx10._ack_validate(VALID)               # implementation = buildable, no capability
    assert err and "capability" in err
    assert gx10._ack_validate({**VALID, "capability": "feat-x"}) is None


def test_gate_disabled_degrades_to_none():
    gx10.ACK_ENABLED = False
    assert gx10._ack_validate({"garbage": 1}) is None


def test_config_toggles_the_gate():
    defaults = gx10._code_defaults()
    assert defaults["ack"]["enabled"] is True
    assert defaults["lodestar"]["enabled"] is False
    cfg = gx10._deep_merge(gx10._code_defaults(),
                           {"ack": {"enabled": False}, "lodestar": {"enabled": True}})
    gx10._apply_config(cfg)
    assert gx10.ACK_ENABLED is False and gx10.LODESTAR_ENABLED is True
