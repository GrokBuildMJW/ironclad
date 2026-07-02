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
    # A plugin/non-core LEAF under a live root: _apply_config may raise — the dict write must still stand
    # (#932 gap-2: the root 'mpr' exists, so it is a known namespace, not a rejected unknown-root typo).
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", {"mpr": {}})
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


# ── #932: discovery (/config keys), the unknown-root guard (gap-2), tiers + tool params ───────────────
def test_cfg_flatten_keys():
    assert set(gx10._cfg_flatten_keys({"a": {"b": 1, "c": {"d": 2}}, "e": 3})) == {"a.b", "a.c.d", "e"}


def test_dispatch_config_keys_lists_keys_with_boot_only_flag(monkeypatch, captured):
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG",
                        {"context": {"rag_enabled": True}, "security": {"profile": "open"}})
    gx10._dispatch(types.SimpleNamespace(), "config keys")
    blob = "\n".join(captured)
    assert "context.rag_enabled" in blob
    assert "security.profile" in blob and "boot-only" in blob      # a frozen key is flagged
    assert "= True" in blob and "(bool)" in blob                   # #956: current value + inferred type


def test_dispatch_config_set_rejects_unknown_root(monkeypatch, captured):
    # #932 gap-2: a typo'd root section is refused — no silent write, no false-GREEN.
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", {"context": {"rag_enabled": True}})
    gx10._dispatch(types.SimpleNamespace(), "config set contextt.rag_enabled on")   # typo root
    assert "contextt" not in gx10._EFFECTIVE_CFG                    # nothing written
    assert any("unknown key" in s for s in captured)               # explicit refusal, not GREEN 'set'
    assert not any("set contextt" in s for s in captured)


def test_render_command_tiers_groups_by_danger():
    out = gx10._render_command_tiers()
    assert "read-only" in out and "destructive" in out
    assert "project" in out and "help" in out                      # destructive vs read-only, from the spec


def test_catalogue_snapshot_tool_params(monkeypatch):
    # #932: /skills surfaces a tool's parameters (so /tool <name> is callable without reading the schema).
    schema = {"function": {"name": "demo_tool", "description": "d",
                           "parameters": {"required": ["query"], "properties": {"query": {}, "n": {}}}}}
    monkeypatch.setattr(gx10, "_PLUGIN_TOOLS", {"demo_tool": {"schema": schema}})
    snap = gx10._catalogue_snapshot()
    tool = next(s for s in snap["skills"] if s["name"] == "demo_tool")
    assert tool["params"] == ["query"]


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


# ── #956: engine i18n of the remaining command-ergonomics chrome + single-language confirm ────────────
def test_config_keys_and_tiers_localize_via_msg(monkeypatch, captured):
    # the new engine outputs route through _msg → a DE overlay renders (EN is the source/default)
    monkeypatch.setattr(gx10, "LANGUAGE", "de", raising=False)
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", {"context": {"rag_enabled": True}})
    gx10._dispatch(types.SimpleNamespace(), "config keys")
    assert any("Config-Keys" in s for s in captured)                # German header
    captured.clear()
    tiers = gx10._render_command_tiers()
    assert "Gefahren-Stufe" in tiers and "destruktiv" in tiers       # German tier header + label
    captured.clear()
    gx10._dispatch(types.SimpleNamespace(), "config set nope.leaf on")
    assert any("unbekannter Key" in s for s in captured)             # German unknown-root refusal


def test_engine_i18n_keys_present_in_both_langs():
    import importlib
    m = importlib.import_module("messages")
    for k in ("keys.header", "keys.boot_only", "tiers.header", "tiers.read_only", "tiers.mutating",
              "tiers.costly", "tiers.destructive", "config.unknown_key", "skills.params"):
        assert k in m._MESSAGES["en"] and k in m._MESSAGES["de"], f"missing {k}"
