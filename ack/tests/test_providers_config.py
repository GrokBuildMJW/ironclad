"""P0-6: the `providers` config block + GX10_PROVIDERS* env overrides (gx10.py §7).

Default is EMPTY/OFF → load_registry → None → parallel_reason stays on _WORKERS.fanout (byte-identical).
The env switches make the router activatable (GX10_PROVIDERS=1 is the documented A/B switch).
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=object))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402
from providers import load_registry  # noqa: E402

_PROVIDER_ENV = ["GX10_PROVIDERS", "GX10_PROVIDERS_DEFAULT", "GX10_PROVIDERS_BUDGET_USD",
                 "GX10_PROVIDERS_MAX_AGENTS", "GX10_PROVIDERS_CLI_TIMEOUT_S"]


def _clear(monkeypatch):
    for k in _PROVIDER_ENV:
        monkeypatch.delenv(k, raising=False)


def test_providers_default_off_and_empty_pool():
    p = gx10._code_defaults()["providers"]
    assert p["enabled"] is False
    assert p["pool"] == []                       # no hard-coded providers (boundary)
    assert p["budget"] == {"usd_cap": None}
    assert load_registry(gx10._code_defaults()) is None   # empty pool → inactive → byte-identical


def test_no_env_leaves_router_off(monkeypatch):
    _clear(monkeypatch)
    cfg = gx10._apply_env(gx10._code_defaults())
    assert cfg["providers"]["enabled"] is False  # default path unchanged


def test_env_enables_and_sets(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("GX10_PROVIDERS", "1")
    monkeypatch.setenv("GX10_PROVIDERS_DEFAULT", "spark-vllm")
    monkeypatch.setenv("GX10_PROVIDERS_BUDGET_USD", "0.5")
    monkeypatch.setenv("GX10_PROVIDERS_MAX_AGENTS", "6")
    monkeypatch.setenv("GX10_PROVIDERS_CLI_TIMEOUT_S", "30")
    cfg = gx10._apply_env(gx10._code_defaults())
    p = cfg["providers"]
    assert p["enabled"] is True                  # GX10_PROVIDERS=1 → A/B switch on
    assert p["default_id"] == "spark-vllm"
    assert p["budget"] == {"usd_cap": 0.5}
    assert p["max_agents"] == 6
    assert p["cli_timeout_s"] == 30


def test_invalid_budget_is_failsoft(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("GX10_PROVIDERS_BUDGET_USD", "not-a-number")  # transform raises → warn + ignore
    cfg = gx10._apply_env(gx10._code_defaults())
    assert cfg["providers"]["budget"] == {"usd_cap": None}           # unchanged, no crash
