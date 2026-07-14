"""Provider config keeps setup.type as the single topology authority (#1468 F7)."""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=object))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402
from providers import load_registry  # noqa: E402

_PROVIDER_ENV = ["GX10_PROVIDERS", "GX10_PROVIDERS_DEFAULT", "GX10_PROVIDERS_BUDGET_USD",
                 "GX10_PROVIDERS_MAX_AGENTS", "GX10_PROVIDERS_CLI_TIMEOUT_S"]


def _clear(monkeypatch):
    for key in _PROVIDER_ENV:
        monkeypatch.delenv(key, raising=False)


def test_provider_defaults_have_no_dead_switches_and_empty_pool():
    providers = gx10._code_defaults()["providers"]
    assert "enabled" not in providers
    assert "scoring" not in providers
    assert providers["pool"] == []
    assert providers["budget"] == {"usd_cap": None}
    assert load_registry(gx10._code_defaults()) is None


def test_setup_type_alone_derives_provider_enablement():
    server = gx10._code_defaults()
    local = gx10._code_defaults()
    local["setup"]["type"] = "local"
    local["connection"]["base_url"] = "http://model.example/v1"

    assert gx10.resolve_offload_topology(server)["providers_enabled"] is False
    assert gx10.resolve_offload_topology(local)["providers_enabled"] is True


def test_retired_provider_env_warns_and_does_not_create_config_switch(monkeypatch, capsys):
    _clear(monkeypatch)
    monkeypatch.setenv("GX10_PROVIDERS", "1")

    cfg = gx10._apply_env(gx10._code_defaults())

    assert "GX10_PROVIDERS" in capsys.readouterr().out
    assert "enabled" not in cfg["providers"]


def test_surviving_provider_env_overrides_still_apply(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("GX10_PROVIDERS_DEFAULT", "spark-vllm")
    monkeypatch.setenv("GX10_PROVIDERS_BUDGET_USD", "0.5")
    monkeypatch.setenv("GX10_PROVIDERS_MAX_AGENTS", "6")
    monkeypatch.setenv("GX10_PROVIDERS_CLI_TIMEOUT_S", "30")
    providers = gx10._apply_env(gx10._code_defaults())["providers"]
    assert providers["default_id"] == "spark-vllm"
    assert providers["budget"] == {"usd_cap": 0.5}
    assert providers["max_agents"] == 6
    assert providers["cli_timeout_s"] == 30


def test_invalid_budget_is_failsoft(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("GX10_PROVIDERS_BUDGET_USD", "not-a-number")
    cfg = gx10._apply_env(gx10._code_defaults())
    assert cfg["providers"]["budget"] == {"usd_cap": None}


@pytest.mark.parametrize(
    ("dotted", "value"),
    [("providers.enabled", False), ("providers.enabled", True), ("providers.enabled", "anything"),
     ("providers.scoring.w_cost", 9.0),
     ("providers.scoring.future_weight", "anything")],
)
def test_retired_provider_config_warns_is_ignored_and_runtime_set_is_refused(
        monkeypatch, capsys, dotted, value):
    cfg = gx10._code_defaults()
    gx10._cfg_set(cfg, dotted, value)

    gx10._apply_config(cfg)

    assert dotted.split(".", 2)[0] in capsys.readouterr().out
    assert "enabled" not in cfg["providers"]
    assert "scoring" not in cfg["providers"]

    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", cfg)
    surfaced = []
    monkeypatch.setattr(gx10, "_ui_print", lambda message, *a, **k: surfaced.append(str(message)))
    gx10._dispatch(None, f"config set {dotted} {value}")
    assert len(surfaced) == 1 and "retired and cannot be set" in surfaced[0]
