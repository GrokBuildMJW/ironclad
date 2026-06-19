"""Provider registry (engine/providers.py) — P0 backend pool schema + loading.

Pure schema/validation tests (no model, no network): valid pool loads, dupes/kind/default fail loud,
slug guard, disabled excluded, and the empty-config → None fallback (byte-identical to today's path).
"""
from __future__ import annotations

import sys
from pathlib import Path

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import pytest  # noqa: E402
from pydantic import ValidationError  # noqa: E402
from providers import (  # noqa: E402
    ProviderKind,
    ProviderSpec,
    load_registry,
)

SPARK = {
    "provider_id": "spark-vllm", "kind": "in-engine", "model": "qwen3.6-35b",
    "endpoint_env": "GX10_BASE_URL", "capabilities": {"local": True},
}
SONNET = {
    "provider_id": "claude-sonnet", "kind": "cli", "model": "sonnet",
    "bin": "claude", "cmd_template": "{bin} --model {model} --print {prompt}",
}


def test_load_registry_empty_returns_none():
    # No pool configured → None → caller falls back byte-identically to direct _WORKERS.fanout.
    assert load_registry({}) is None
    assert load_registry({"providers": {}}) is None
    assert load_registry({"providers": {"pool": []}}) is None


def test_valid_pool_loads_and_validates():
    reg = load_registry({"providers": {"pool": [SPARK, SONNET], "default_id": "spark-vllm"}})
    assert reg is not None
    assert set(reg.by_id()) == {"spark-vllm", "claude-sonnet"}
    assert reg.default_id == "spark-vllm"
    assert reg.by_id()["spark-vllm"].kind == ProviderKind.IN_ENGINE
    assert reg.by_id()["claude-sonnet"].kind == ProviderKind.CLI


def test_duplicate_provider_id_raises():
    with pytest.raises(ValueError, match="duplicate provider_id"):
        load_registry({"providers": {"pool": [SPARK, dict(SPARK)]}})


def test_cli_requires_cmd_or_bin():
    bad = {"provider_id": "x", "kind": "cli", "model": "m"}  # no cmd_template/bin
    with pytest.raises(ValueError, match="kind=cli requires"):
        load_registry({"providers": {"pool": [bad]}})


def test_in_engine_requires_endpoint_env():
    bad = {"provider_id": "y", "kind": "in-engine", "model": "m"}  # no endpoint_env
    with pytest.raises(ValueError, match="kind=in-engine requires"):
        load_registry({"providers": {"pool": [bad]}})


def test_default_id_must_be_in_pool():
    with pytest.raises(ValueError, match="default_id"):
        load_registry({"providers": {"pool": [SPARK], "default_id": "nope"}})


def test_provider_id_slug_guard():
    with pytest.raises((ValueError, ValidationError)):
        ProviderSpec(provider_id="a b", kind="in-engine", model="m", endpoint_env="E")
    with pytest.raises((ValueError, ValidationError)):
        ProviderSpec(provider_id="", kind="in-engine", model="m", endpoint_env="E")


def test_by_id_excludes_disabled():
    reg = load_registry({"providers": {"pool": [SPARK, {**SONNET, "enabled": False}]}})
    assert reg is not None
    assert set(reg.by_id()) == {"spark-vllm"}  # disabled provider filtered out


def test_endpoint_value_comes_from_env_name_not_config():
    # §9 boundary: a ProviderSpec stores only the ENV *name* (endpoint_env/api_key_env), never a
    # resolved base_url/key value — so no secret/host literal can live in core/ config.
    p = ProviderSpec(**SPARK)
    assert p.endpoint_env == "GX10_BASE_URL"
    fields = set(ProviderSpec.model_fields)
    assert "base_url" not in fields and "api_key" not in fields and "endpoint" not in fields


def test_spec_defaults():
    p = ProviderSpec(**SPARK)
    assert p.capabilities.reasoning is True
    assert p.capabilities.local is True
    assert p.rate_limit.max_concurrent == 4
    assert p.weight == 100
    assert p.enabled is True
    assert p.cost_per_1k_in == 0.0  # local = free on the cost axis
