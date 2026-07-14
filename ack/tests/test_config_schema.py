"""F6a typed configuration-schema and strict boundary contract."""
from __future__ import annotations

import copy
import json
import math
import sys
import types
from pathlib import Path

import pytest

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=object))
_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import command_spec  # noqa: E402
import config_schema as schema  # noqa: E402
import gx10  # noqa: E402


def test_schema_is_non_vacuous_and_every_effective_leaf_has_complete_metadata():
    assert len(schema.LEAVES) > 100
    for key, leaf in schema.LEAVES.items():
        assert leaf.key == key and "." in key
        assert leaf.python_type
        assert leaf.lifecycle in {schema.RUNTIME, schema.BOOT_ONLY}
        assert leaf.classification in {schema.SWITCH, schema.TUNING}
        assert leaf.secret_policy in {schema.PUBLIC, schema.ENV_NAME, schema.REDACT}
        assert callable(leaf.env_parser)


def test_code_defaults_are_schema_derived_and_include_surviving_roots():
    expected = schema.defaults_tree()
    assert gx10._code_defaults() == expected
    assert gx10._code_defaults() is not expected
    assert {"framing_notes", "automation", "heartbeat", "memory", "warm"} <= expected.keys()
    assert expected["ace"]["fork_mpr"]["enabled"] is False
    assert expected["paths"]["code_subdir"] == ""


def test_secure_deployment_defaults_and_explicit_feature_enablement():
    cfg = schema.defaults_tree()
    assert cfg["server"]["host"] == "127.0.0.1"
    assert cfg["security"]["allow_unauthenticated_bind"] is False
    assert cfg["search"]["enabled"] is False
    assert cfg["forge"]["enabled"] is False

    cfg["search"]["enabled"] = True
    cfg["forge"]["enabled"] = True
    assert schema.validate(cfg) is cfg


def test_external_writer_limits_are_finite_bounded_and_reject_unlimited_values():
    cfg = schema.defaults_tree()
    assert cfg["connection"]["connect_timeout_s"] == 10.0
    assert cfg["connection"]["first_token_timeout_s"] == 600.0
    assert cfg["providers"]["cli_timeout_s"] == 900.0
    assert cfg["workers"]["concurrency"] == 4
    assert cfg["autopilot"]["autoplan_max_tasks"] == 20
    assert all(value is not None and value > 0 for value in (
        cfg["connection"]["connect_timeout_s"],
        cfg["connection"]["first_token_timeout_s"],
        cfg["providers"]["cli_timeout_s"],
    ))

    for key, value in (
        ("autopilot.max_concurrent", 0),
        ("autopilot.autoplan_max_tasks", 0),
        ("connection.connect_timeout_s", 121),
        ("connection.first_token_timeout_s", 1801),
        ("providers.cli_timeout_s", 3601),
        ("workers.concurrency", 65),
        ("strategy.budget", 4),
    ):
        with pytest.raises(schema.ConfigError):
            schema.validate_leaf(key, value)

    for key, value in (
        ("autopilot.max_concurrent", 4),
        ("autopilot.autoplan_max_tasks", 50),
        ("connection.connect_timeout_s", 30),
        ("connection.first_token_timeout_s", 900),
        ("providers.cli_timeout_s", 1200),
        ("workers.concurrency", 8),
    ):
        assert schema.validate_leaf(key, value) == value


@pytest.mark.parametrize("value", ["false", "true", 0, 1, None, [], {}])
def test_file_and_merged_bool_values_require_exact_bool(value):
    with pytest.raises(schema.ConfigError, match="expected bool"):
        schema.validate_leaf("context.rag_enabled", value)


@pytest.mark.parametrize("value", ["false", "true", 0, 1, None])
def test_json_bool_lookalikes_are_rejected_after_file_merge(tmp_path, value):
    source = tmp_path / "config.json"
    source.write_text(json.dumps({"context": {"rag_enabled": value}}), encoding="utf-8")
    merged = gx10._deep_merge(schema.defaults_tree(), gx10._load_config_tree(source))
    with pytest.raises(schema.ConfigError, match="context.rag_enabled: expected bool"):
        schema.validate(merged)


@pytest.mark.parametrize("payload", [[], None, 1, "config"])
def test_config_file_root_must_be_an_object(tmp_path, payload):
    source = tmp_path / "config.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(schema.ConfigError, match="expected object"):
        gx10._load_config_tree(source)


def test_sorted_multi_profile_tree_is_validated_as_one_merged_projection(tmp_path):
    (tmp_path / "10-context.json").write_text(
        json.dumps({"context": {"max_ctx_chars": 1000}}), encoding="utf-8"
    )
    nested = tmp_path / "context"
    nested.mkdir()
    (nested / "20-trim.json").write_text(
        json.dumps({"context": {"trim_target_chars": 1000}}), encoding="utf-8"
    )
    merged = gx10._deep_merge(schema.defaults_tree(), gx10._load_config_tree(tmp_path))
    with pytest.raises(schema.ConfigError, match="trim_target_chars"):
        schema.validate(merged)


def test_malformed_multi_profile_boot_apply_leaves_config_and_runtime_unpublished(
        tmp_path, monkeypatch):
    (tmp_path / "10-context.json").write_text(
        json.dumps({"context": {"max_ctx_chars": 1000}}), encoding="utf-8"
    )
    nested = tmp_path / "context"
    nested.mkdir()
    (nested / "20-trim.json").write_text(
        json.dumps({"context": {"trim_target_chars": 1000}}), encoding="utf-8"
    )
    merged = gx10._deep_merge(schema.defaults_tree(), gx10._load_config_tree(tmp_path))
    live = schema.defaults_tree()
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", live)
    names = gx10._CONFIG_DERIVED_GLOBALS + gx10._CONFIG_RECONFIG_GLOBALS
    globals_before = {name: getattr(gx10, name) for name in names}

    with pytest.raises(schema.ConfigError, match="trim_target_chars"):
        gx10._apply_config(merged)

    assert gx10._EFFECTIVE_CFG is live
    assert all(getattr(gx10, name) is value for name, value in globals_before.items())


def test_apply_config_validates_before_deriving_runtime_state():
    cfg = schema.defaults_tree()
    cfg["autopilot"]["enabled"] = "false"
    before = gx10.AUTOPILOT_ENABLED
    with pytest.raises(schema.ConfigError, match="autopilot.enabled: expected bool"):
        gx10._apply_config(cfg)
    assert gx10.AUTOPILOT_ENABLED is before


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_non_finite_numbers_are_rejected(value):
    with pytest.raises(schema.ConfigError, match="finite"):
        schema.validate_leaf("quality.threshold", value)


def test_wrong_container_unknown_leaf_range_and_relationship_are_rejected():
    cfg = schema.defaults_tree()
    cfg["context"] = []
    with pytest.raises(schema.ConfigError, match="context: expected dict"):
        schema.validate(cfg)

    cfg = schema.defaults_tree()
    cfg["context"]["unknown"] = 1
    with pytest.raises(schema.ConfigError, match="unknown configuration leaf"):
        schema.validate(cfg)

    cfg = schema.defaults_tree()
    cfg.pop("automation")
    with pytest.raises(schema.ConfigError, match="missing required root.*automation"):
        schema.validate(cfg)

    with pytest.raises(schema.ConfigError, match="> 0"):
        schema.validate_leaf("heartbeat.stall_seconds", 0)

    cfg = schema.defaults_tree()
    cfg["context"]["trim_target_chars"] = cfg["context"]["max_ctx_chars"]
    with pytest.raises(schema.ConfigError, match="trim_target_chars"):
        schema.validate(cfg)


@pytest.mark.parametrize("raw,expected", [
    ("true", True), ("TRUE", True), ("1", True), ("yes", True), ("On", True),
    ("false", False), ("FALSE", False), ("0", False), ("no", False), ("Off", False),
])
def test_env_bool_parser_accepts_only_documented_spellings(raw, expected):
    assert schema.parse_env_bool(raw) is expected


@pytest.mark.parametrize("raw", ["", "enabled", "2", "y", "n", "none", " true-ish "])
def test_env_bool_parser_rejects_every_other_spelling(raw):
    with pytest.raises(schema.ConfigError, match="true/false/1/0/yes/no/on/off"):
        schema.parse_env_bool(raw)


def test_apply_env_warns_and_ignores_invalid_bool(monkeypatch, capsys):
    monkeypatch.setenv("GX10_CONTEXT_RAG", "sometimes")
    cfg = schema.defaults_tree()
    assert gx10._apply_env(cfg)["context"]["rag_enabled"] is True
    assert "ignored" in capsys.readouterr().out


def test_apply_env_parses_documented_false_before_validation(monkeypatch):
    monkeypatch.setenv("GX10_CONTEXT_RAG", "OFF")
    cfg = gx10._apply_env(schema.defaults_tree())
    assert cfg["context"]["rag_enabled"] is False
    assert type(cfg["context"]["rag_enabled"]) is bool


def test_as_bool_rejects_integer_and_string_truthiness():
    assert gx10._as_bool(False) is False
    for value in (1, 0, "true", "false"):
        with pytest.raises(schema.ConfigError, match="expected bool"):
            gx10._as_bool(value)


@pytest.mark.parametrize("mode", ["reducer", "direct"])
def test_workers_write_mode_accepts_only_supported_enum(mode):
    assert schema.validate_leaf("workers.write_mode", mode) == mode


def test_workers_write_mode_unknown_string_is_refused():
    with pytest.raises(schema.ConfigError, match="expected one of"):
        schema.validate_leaf("workers.write_mode", "shared")


def test_multi_tenant_true_is_refused_until_enforcement_is_complete():
    assert schema.validate_leaf("security.multi_tenant", False) is False
    with pytest.raises(schema.ConfigError, match="cannot be enabled"):
        schema.validate_leaf("security.multi_tenant", True)


def test_boot_only_metadata_has_exact_runtime_and_command_spec_parity():
    required = {
        "setup.type", "server.host", "security.profile", "security.web_in_sealed",
        "security.allow_unauthenticated_bind",
        "search.enabled", "search.adapter", "search.api_key_env",
        "security.token_env", "security.session_heartbeat_s", "security.code_locality",
        "workers.concurrency", "workers.max_tokens", "workers.max_batch_tokens",
        "providers.pool", "providers.default_id", "providers.max_agents",
        "providers.cli_timeout_s", "providers.effort_max_tokens",
        "security.tooling_envelope.allow_list", "alert.enabled", "alert.interval_s",
        "watcher.interval", "tasks.id_prefix", "paths.state_root", "paths.vault_root",
        "paths.session_file", "paths.code_root", "paths.code_subdir", "paths.workdir",
        "paths.plugins_dir", "paths.post_advance_hooks",
    }
    assert required <= schema.BOOT_ONLY_KEYS
    assert schema.BOOT_ONLY_KEYS == gx10._FROZEN_CONFIG_KEYS
    assert schema.BOOT_ONLY_KEYS == command_spec.SPEC_FROZEN_CONFIG_KEYS


def test_tombstone_is_metadata_not_a_live_leaf():
    assert schema.LEAVES and schema.TOMBSTONES
    retired = "design_gate.enabled"
    assert retired in schema.TOMBSTONES
    assert retired not in schema.LEAVES
    assert retired not in {key for key in _flatten(schema.defaults_tree())}


def _flatten(node, prefix=""):
    for key, value in node.items():
        dotted = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict) and value:
            yield from _flatten(value, dotted)
        else:
            yield dotted


def test_validate_accepts_a_fresh_complete_default_projection():
    cfg = copy.deepcopy(schema.defaults_tree())
    assert schema.validate(cfg) is cfg


def test_validate_allows_only_explicitly_known_plugin_roots():
    cfg = schema.defaults_tree()
    cfg["mpr"] = {"enabled": True}
    with pytest.raises(schema.ConfigError, match="unknown configuration leaf"):
        schema.validate(cfg)
    assert schema.validate(cfg, plugin_roots=("mpr",)) is cfg


@pytest.mark.parametrize("value", ["server", "local", "auto"])
def test_setup_type_accepts_auto_alongside_server_local(value):
    # BLOCKER regression (#1467 whole-change review): "auto" (INSTALL-1 #503) is a first-class setup.type —
    # the desktop installer ships GX10_SETUP_TYPE=auto and resolve_offload_topology derives server/local
    # from the base_url at boot. The typed schema must accept it (dropping it degraded env→server silently
    # and crashed a config-file boot). The schema enum must stay a superset of gx10._VALID_SETUP_TYPES.
    assert value in gx10._VALID_SETUP_TYPES
    assert schema.validate_leaf("setup.type", value) == value
    tree = schema.defaults_tree()
    tree["setup"]["type"] = value
    assert schema.validate(tree) is tree           # a merged projection with setup.type=auto must not refuse


def test_apply_env_setup_type_auto_is_honored(monkeypatch):
    monkeypatch.setenv("GX10_SETUP_TYPE", "auto")
    cfg = gx10._apply_env(schema.defaults_tree())
    assert cfg["setup"]["type"] == "auto"          # not warned-and-discarded by an over-strict enum
