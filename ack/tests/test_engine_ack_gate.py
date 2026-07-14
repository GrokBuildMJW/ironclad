"""Mandatory engine-to-ACK validation at the staging boundary (#1466 F5a)."""
from __future__ import annotations

import builtins
import sys
import types
from pathlib import Path

import pytest

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=object))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402

VALID = {"type": "implementation", "priority": "high", "title": "task title", "description": "task body"}


@pytest.fixture(autouse=True)
def _restore_lodestar():
    before = gx10.LODESTAR_ENABLED
    yield
    gx10.LODESTAR_ENABLED = before


def test_valid_base_task_passes_mandatory_ack():
    assert gx10._ack_validate(VALID) is None


def test_missing_required_field_returns_exact_validation_error():
    err = gx10._ack_validate({"type": "implementation", "priority": "high", "title": "task title"})
    assert err and "description" in err


def test_extra_key_is_rejected():
    assert gx10._ack_validate({**VALID, "bogus": "x"})


def test_lodestar_remains_an_optional_stricter_schema_selector():
    gx10.LODESTAR_ENABLED = True
    err = gx10._ack_validate(VALID)
    assert err and "capability" in err
    assert gx10._ack_validate({**VALID, "capability": "feat-x"}) is None


def test_validator_import_unavailable_refuses_fail_closed(monkeypatch):
    real_import = builtins.__import__

    def unavailable(name, *args, **kwargs):
        if name == "ack.case_spec":
            raise ImportError("validator unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", unavailable)
    err = gx10._ack_validate(VALID)
    assert err and "validator unavailable" in err and "fail-closed" in err


@pytest.mark.parametrize("legacy", [True, False], ids=["legacy-true", "legacy-false"])
def test_ack_enabled_is_a_warning_only_tombstone_and_cannot_disable(legacy, capsys):
    cfg = gx10._code_defaults()
    assert "ack" not in cfg and not hasattr(gx10, "ACK_ENABLED")
    cfg["ack"] = {"enabled": legacy}

    gx10._apply_config(cfg)
    gx10._apply_config(cfg)

    warnings = [line for line in capsys.readouterr().out.splitlines() if "DEPRECATED" in line]
    assert len(warnings) == 1
    assert "ack.enabled" in warnings[0] and "always on" in warnings[0]
    assert "ack" not in cfg
    assert gx10._ack_validate({"garbage": 1}) is not None


def test_runtime_set_refuses_retired_ack_switch(monkeypatch):
    cfg = gx10._code_defaults()
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", cfg)
    surfaced = []
    monkeypatch.setattr(gx10, "_ui_print", lambda message, *a, **k: surfaced.append(str(message)))

    gx10._dispatch(None, "config set ack.enabled false")

    assert len(surfaced) == 1 and "retired and cannot be set" in surfaced[0]
    assert "ack" not in cfg
