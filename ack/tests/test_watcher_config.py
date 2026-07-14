"""Watcher runtime authority belongs only to /auto (#1468 F7)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402


@pytest.mark.parametrize("value", [True, False, "anything"])
def test_retired_watcher_config_warns_is_ignored_and_does_not_change_runtime(capsys, value):
    cfg = gx10._code_defaults()
    assert "enabled" not in cfg["watcher"]
    before = gx10._WATCHER_ENABLED
    cfg["watcher"]["enabled"] = value

    gx10._apply_config(cfg)

    assert "watcher.enabled" in capsys.readouterr().out
    assert "enabled" not in cfg["watcher"]
    assert gx10._WATCHER_ENABLED is before


def test_runtime_set_refuses_retired_watcher_switch(monkeypatch):
    cfg = gx10._code_defaults()
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", cfg)
    surfaced = []
    monkeypatch.setattr(gx10, "_ui_print", lambda message, *a, **k: surfaced.append(str(message)))

    gx10._dispatch(None, "config set watcher.enabled true")

    assert "enabled" not in cfg["watcher"]
    assert len(surfaced) == 1 and "retired and cannot be set" in surfaced[0]


def test_config_display_reads_live_watcher_state_not_config(monkeypatch):
    cfg = gx10._code_defaults()
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", cfg)
    monkeypatch.setattr(gx10, "_WATCHER_ENABLED", True)

    rendered = gx10._render_config()

    assert "watcher       : enabled=True" in rendered
    assert "enabled" not in cfg["watcher"]
