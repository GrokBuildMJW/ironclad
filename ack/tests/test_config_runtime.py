"""Atomic, plugin-agnostic runtime config control for `/config get|set`."""
from __future__ import annotations

import copy
import math
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


def _complete_live_config(monkeypatch, **extra):
    cfg = gx10._code_defaults()
    cfg.update(copy.deepcopy(extra))
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", cfg)
    return cfg


def _runtime_globals():
    names = gx10._CONFIG_DERIVED_GLOBALS + gx10._CONFIG_RECONFIG_GLOBALS
    return {name: getattr(gx10, name) for name in names}


def _assert_atomic_refusal(captured, original, tree_before, globals_before):
    assert gx10._EFFECTIVE_CFG is original
    assert gx10._EFFECTIVE_CFG == tree_before
    assert all(getattr(gx10, name) is value for name, value in globals_before.items())
    refusals = [line for line in captured if "[config] refused:" in line]
    assert len(refusals) == 1
    assert not any("[config] set " in line for line in captured)


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


@pytest.mark.parametrize("key,raw,original", [
    ("context.rag_enabled", "1", True),
    ("context.rag_enabled", "maybe", True),
    ("workers.write_mode", "shared", "reducer"),
    ("security.multi_tenant", "on", False),
])
def test_dispatch_config_set_refuses_schema_invalid_typed_value(
        monkeypatch, captured, key, raw, original):
    cfg = gx10._code_defaults()
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", cfg)
    gx10._dispatch(types.SimpleNamespace(), f"config set {key} {raw}")
    assert gx10._cfg_get(cfg, key) == original
    assert any("refused" in line for line in captured)
    assert not any(f"set {key}" in line for line in captured)


@pytest.mark.parametrize("key,value", [
    ("context.rag_enabled", "false"),
    ("context.rag_enabled", "true"),
    ("context.rag_enabled", 0),
    ("context.rag_enabled", 1),
    ("context.rag_enabled", None),
    ("quality.threshold", []),
    ("quality.threshold", math.nan),
    ("quality.threshold", math.inf),
    ("quality.threshold", -math.inf),
    ("quality.threshold", 2.0),
])
def test_runtime_set_refuses_invalid_candidate_without_any_mutation(
        monkeypatch, captured, key, value):
    original = _complete_live_config(monkeypatch)
    tree_before = copy.deepcopy(original)
    globals_before = _runtime_globals()
    monkeypatch.setattr(gx10, "_coerce_cfg_value", lambda _raw: value)
    gx10._dispatch(types.SimpleNamespace(), f"config set {key} candidate")
    _assert_atomic_refusal(captured, original, tree_before, globals_before)


def test_runtime_set_refuses_relationship_violation_atomically(monkeypatch, captured):
    original = _complete_live_config(monkeypatch)
    tree_before = copy.deepcopy(original)
    globals_before = _runtime_globals()
    raw = str(original["context"]["max_ctx_chars"])
    gx10._dispatch(types.SimpleNamespace(), f"config set context.trim_target_chars {raw}")
    _assert_atomic_refusal(captured, original, tree_before, globals_before)


def test_runtime_set_refuses_missing_required_root_atomically(monkeypatch, captured):
    original = gx10._code_defaults()
    original.pop("automation")
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", original)
    tree_before = copy.deepcopy(original)
    globals_before = _runtime_globals()
    gx10._dispatch(types.SimpleNamespace(), "config set verify.grounding_threshold 0.6")
    _assert_atomic_refusal(captured, original, tree_before, globals_before)


def test_runtime_set_refuses_unknown_core_leaf_atomically(monkeypatch, captured):
    original = _complete_live_config(monkeypatch)
    tree_before = copy.deepcopy(original)
    globals_before = _runtime_globals()
    gx10._dispatch(types.SimpleNamespace(), "config set context.unknown 1")
    _assert_atomic_refusal(captured, original, tree_before, globals_before)


@pytest.mark.parametrize("stage", ["early", "middle", "late"])
def test_runtime_set_derivation_exception_rolls_back_everything(
        monkeypatch, captured, stage):
    original = _complete_live_config(monkeypatch)
    tree_before = copy.deepcopy(original)
    globals_before = _runtime_globals()

    def boom(*_args, **_kwargs):
        raise RuntimeError(f"{stage} derivation")

    if stage == "early":
        monkeypatch.setattr(gx10, "_resolve_platform", boom)
    elif stage == "middle":
        from ack import tooling_envelope
        monkeypatch.setattr(tooling_envelope, "load_tooling_envelope_policy", boom)
    else:
        monkeypatch.setattr(gx10, "_derive_quality_breaker_state", boom)
    gx10._dispatch(types.SimpleNamespace(), "config set verify.grounding_threshold 0.6")
    _assert_atomic_refusal(captured, original, tree_before, globals_before)


def test_runtime_set_partial_commit_exception_restores_every_global(monkeypatch, captured):
    original = _complete_live_config(monkeypatch)
    tree_before = copy.deepcopy(original)
    globals_before = _runtime_globals()

    def partial_commit(derived):
        gx10.DEFAULT_MODEL = derived.values["DEFAULT_MODEL"]
        gx10._VERIFY_GROUNDING_THRESHOLD = derived.values["_VERIFY_GROUNDING_THRESHOLD"]
        raise RuntimeError("commit interrupted")

    monkeypatch.setattr(gx10, "_commit_config_state", partial_commit)
    gx10._dispatch(types.SimpleNamespace(), "config set verify.grounding_threshold 0.6")
    _assert_atomic_refusal(captured, original, tree_before, globals_before)


def test_runtime_set_hook_exception_restores_hooks_store_and_globals(monkeypatch, captured):
    from ack import hooks

    original = _complete_live_config(monkeypatch)
    tree_before = copy.deepcopy(original)
    globals_before = _runtime_globals()
    hooks_before = dict(hooks._HOOKS)
    marker = lambda _ctx: None

    def fail_after_hook(_cfg, *, strict=False):
        hooks.register_hook("post_handover", marker)
        raise RuntimeError("hook reconfiguration interrupted")

    monkeypatch.setattr(gx10, "_apply_quality_consumer", fail_after_hook)
    gx10._dispatch(types.SimpleNamespace(), "config set verify.grounding_threshold 0.6")
    _assert_atomic_refusal(captured, original, tree_before, globals_before)
    assert hooks._HOOKS == hooks_before


def test_runtime_set_ace_reconfig_exception_stops_new_worker_and_restores(monkeypatch, captured):
    # Contract §1 "no thread left half-reconfigured": _apply_ace is the LAST reconfiguration step and the
    # only one that may start a worker, so its failure AFTER a worker start must stop that worker and
    # restore _ACE_WORKER on rollback (gx10._restore_config_runtime).
    original = _complete_live_config(monkeypatch)
    tree_before = copy.deepcopy(original)
    monkeypatch.setattr(gx10, "_ACE_WORKER", None, raising=False)   # pre-transaction: no live worker
    globals_before = _runtime_globals()
    stopped: list[bool] = []

    class _FakeWorker:
        def stop(self):
            stopped.append(True)

    def start_then_fail(_cfg, *, strict=False):
        gx10._ACE_WORKER = _FakeWorker()                           # a worker "starts" mid-reconfiguration
        raise RuntimeError("ace reconfiguration interrupted after worker start")

    monkeypatch.setattr(gx10, "_apply_ace", start_then_fail)
    gx10._dispatch(types.SimpleNamespace(), "config set verify.grounding_threshold 0.6")
    _assert_atomic_refusal(captured, original, tree_before, globals_before)
    assert stopped == [True]                                       # the newly-started worker was stopped
    assert gx10._ACE_WORKER is None                                # and the global restored to pre-set


def test_valid_runtime_sets_publish_candidate_and_globals_once(monkeypatch, captured):
    original = _complete_live_config(monkeypatch)
    gx10._dispatch(types.SimpleNamespace(), "config set quality.threshold 0.7")
    assert gx10._EFFECTIVE_CFG is not original
    assert original["quality"]["threshold"] == 0.5
    assert gx10._EFFECTIVE_CFG["quality"]["threshold"] == 0.7
    assert gx10._QUALITY_BREAKER.snapshot().threshold == 0.7
    assert sum("[config] set quality.threshold = 0.7" in line for line in captured) == 1
    assert not any("refused" in line for line in captured)

    captured.clear()
    previous = gx10._EFFECTIVE_CFG
    gx10._dispatch(types.SimpleNamespace(), "config set verify.grounding_threshold 0.6")
    assert gx10._EFFECTIVE_CFG is not previous
    assert previous["verify"]["grounding_threshold"] == 0.5
    assert gx10._EFFECTIVE_CFG["verify"]["grounding_threshold"] == 0.6
    assert gx10._VERIFY_GROUNDING_THRESHOLD == 0.6
    assert sum("[config] set verify.grounding_threshold = 0.6" in line for line in captured) == 1
    assert not any("refused" in line for line in captured)


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
def test_dispatch_config_set_commits_plugin_candidate_atomically(monkeypatch, captured):
    original = _complete_live_config(monkeypatch, mpr={})
    gx10._dispatch(types.SimpleNamespace(), "config set mpr.enabled on")
    assert gx10._EFFECTIVE_CFG is not original
    assert "enabled" not in original["mpr"]
    assert gx10._EFFECTIVE_CFG["mpr"]["enabled"] is True
    assert sum("[config] set mpr.enabled = True" in line for line in captured) == 1


def test_dispatch_config_set_rolls_back_plugin_candidate_when_derivation_raises(monkeypatch, captured):
    original = _complete_live_config(monkeypatch, mpr={})
    tree_before = copy.deepcopy(original)
    globals_before = _runtime_globals()
    monkeypatch.setattr(gx10, "_derive_config_state", lambda _cfg: (_ for _ in ()).throw(KeyError("connection")))
    gx10._dispatch(types.SimpleNamespace(), "config set mpr.panel_mode deep")
    _assert_atomic_refusal(captured, original, tree_before, globals_before)


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
    original = _complete_live_config(monkeypatch)
    tree_before = copy.deepcopy(original)
    globals_before = _runtime_globals()
    gx10._dispatch(types.SimpleNamespace(), "config set setup.type local")
    _assert_atomic_refusal(captured, original, tree_before, globals_before)
    assert any("boot-only" in line for line in captured)


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
