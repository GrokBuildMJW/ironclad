"""Generic runtime config control — `/config get|set <dotted.key> <value>` (core, plugin-agnostic).

Covers the value coercion, the dotted-path read/write helpers, and the `_dispatch` branches:
set mutates the live `_EFFECTIVE_CFG` + re-derives core globals via `_apply_config`; a non-core key
(e.g. a plugin section) still stores even when `_apply_config` can't apply it; get reads a dotted key;
set with no live config is a friendly no-op. No plugin-specific knowledge lives in core.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

# Stub the heavy optional dep so the engine imports without openai installed.
sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=object))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402
import pytest  # noqa: E402


@pytest.fixture
def captured(monkeypatch):
    """Route _ui_print to a buffer (headless sink), restored after the test."""
    lines: list[str] = []
    monkeypatch.setattr(gx10, "_UI_APP", None, raising=False)
    monkeypatch.setattr(gx10, "_UI_SINK", lambda s: lines.append(s))
    return lines


# ── value coercion ────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw,expected", [
    ("on", True), ("ON", True), ("true", True), ("yes", True),
    ("off", False), ("Off", False), ("false", False), ("no", False),
    ("5", 5), ("-3", -3), ("1.5", 1.5), ("deep", "deep"), ("claude-sonnet", "claude-sonnet"),
])
def test_coerce_cfg_value(raw, expected):
    out = gx10._coerce_cfg_value(raw)
    assert out == expected and type(out) is type(expected)


# ── dotted read/write helpers ───────────────────────────────────────────────────────────────────────
def test_cfg_set_creates_nested_sections():
    cfg: dict = {}
    gx10._cfg_set(cfg, "mpr.enabled", True)
    gx10._cfg_set(cfg, "mpr.panel_mode", "deep")
    assert cfg == {"mpr": {"enabled": True, "panel_mode": "deep"}}


def test_cfg_set_overwrites_non_dict_intermediate():
    cfg = {"mpr": 1}                       # a scalar where we need to descend
    gx10._cfg_set(cfg, "mpr.enabled", True)
    assert cfg == {"mpr": {"enabled": True}}


def test_cfg_get_reads_and_misses():
    cfg = {"mpr": {"enabled": True}}
    assert gx10._cfg_get(cfg, "mpr.enabled") is True
    assert gx10._cfg_get(cfg, "mpr.missing") is None
    assert gx10._cfg_get(cfg, "nope.deep") is None


# ── _dispatch branches ──────────────────────────────────────────────────────────────────────────────
def test_dispatch_config_set_mutates_and_reapplies(monkeypatch, captured):
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", {"mpr": {}})
    applied: list = []
    monkeypatch.setattr(gx10, "_apply_config", lambda cfg: applied.append(cfg))
    gx10._dispatch(types.SimpleNamespace(), "config set mpr.enabled on")
    assert gx10._EFFECTIVE_CFG["mpr"]["enabled"] is True       # live tree mutated
    assert applied and applied[0] is gx10._EFFECTIVE_CFG        # core globals re-derived
    assert any("set mpr.enabled = True" in s for s in captured)


def test_dispatch_config_set_stores_even_when_apply_raises(monkeypatch, captured):
    # A plugin/non-core key: _apply_config may raise — the dict write must still stand.
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", {})
    def _boom(cfg):
        raise KeyError("connection")
    monkeypatch.setattr(gx10, "_apply_config", _boom)
    gx10._dispatch(types.SimpleNamespace(), "config set mpr.panel_mode deep")
    assert gx10._EFFECTIVE_CFG["mpr"]["panel_mode"] == "deep"
    assert any("stored" in s for s in captured)               # graceful note, no crash


def test_dispatch_config_set_no_live_config(monkeypatch, captured):
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", None)
    gx10._dispatch(types.SimpleNamespace(), "config set mpr.enabled on")
    assert any("no live config" in s for s in captured)

def test_dispatch_config_set_usage(monkeypatch, captured):
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", {})
    gx10._dispatch(types.SimpleNamespace(), "config set mpr.enabled")   # missing value
    assert any("usage:" in s for s in captured)


def test_dispatch_config_get(monkeypatch, captured):
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", {"mpr": {"enabled": True}})
    gx10._dispatch(types.SimpleNamespace(), "config get mpr.enabled")
    assert any("mpr.enabled = True" in s for s in captured)
    captured.clear()
    gx10._dispatch(types.SimpleNamespace(), "config get mpr.missing")
    assert any("not set" in s for s in captured)


def test_dispatch_config_get_no_key_shows_usage(monkeypatch, captured):
    # bare `config get` (clients .trim() the body) must print a usage hint, NOT fall through to a model
    # turn — the SimpleNamespace agent has no .run, so reaching the else would raise AttributeError.
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", {"mpr": {"enabled": True}})
    gx10._dispatch(types.SimpleNamespace(), "config get")
    assert any("usage: /config get" in s for s in captured)


def test_dispatch_config_get_trailing_space_no_indexerror(monkeypatch, captured):
    # a raw `config get ` (trailing space, no key) once matched the branch and raised IndexError on
    # split(None, 2)[2]; it must now print the usage hint instead.
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", {"mpr": {"enabled": True}})
    gx10._dispatch(types.SimpleNamespace(), "config get ")
    assert any("usage: /config get" in s for s in captured)


# ── ST-1: frozen (boot-only) keys ───────────────────────────────────────────────────────────────────
def test_config_set_frozen_key_refused(monkeypatch, captured):
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", {"setup": {"type": "solo"}})
    gx10._dispatch(types.SimpleNamespace(), "config set setup.type pull")
    assert any("boot-only" in s for s in captured)               # refused with a clear message
    assert gx10._EFFECTIVE_CFG["setup"]["type"] == "solo"        # value unchanged


def test_config_get_frozen_key_allowed(monkeypatch, captured):
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", {"setup": {"type": "colocated"}})
    gx10._dispatch(types.SimpleNamespace(), "config get setup.type")
    assert any("setup.type = 'colocated'" in s for s in captured)


def test_security_profile_is_frozen(monkeypatch, captured):
    # security.profile wires the trust policy + bind host once at boot → boot-only, /config set refused
    assert "security.profile" in gx10._FROZEN_CONFIG_KEYS
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", {"security": {"profile": "open"}})
    gx10._dispatch(types.SimpleNamespace(), "config set security.profile sealed")
    assert any("boot-only" in s for s in captured)
    assert gx10._EFFECTIVE_CFG["security"]["profile"] == "open"   # unchanged
