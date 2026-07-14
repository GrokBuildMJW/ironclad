"""Epic #505, S8 — the web-search config + secret surface.

Covers the `search.*` defaults, the `GX10_SEARCH_*` env overrides, the frozen (boot-only) keys, the
config-driven max-output cap + native count, and the invariant that the base wheel stays
dependency-light (no HTTP dependency crept in — Fork 1 is stdlib-only). The API key VALUE is never
config; only its env-var NAME is. No live network.
"""
from __future__ import annotations

import pathlib
import re
import sys

_ENGINE = pathlib.Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402
from websearch_adapters import MockAdapter, _SOURCES_REMINDER, build_web_search_adapter  # noqa: E402
from websearch_brave import BraveAdapter  # noqa: E402


def test_search_defaults_present():
    s = gx10._code_defaults()["search"]
    assert s["enabled"] is False and s["adapter"] == "cli"
    assert s["api_key_env"] == "GX10_SEARCH_API_KEY"        # NAME only, never the secret
    assert s["count"] == 10 and s["max_output_chars"] == 100_000


def test_search_env_overrides_apply(monkeypatch):
    monkeypatch.setenv("GX10_SEARCH_ADAPTER", "mock")
    monkeypatch.setenv("GX10_SEARCH_COUNT", "5")
    monkeypatch.setenv("GX10_SEARCH_MAX_OUTPUT_CHARS", "1234")
    monkeypatch.setenv("GX10_SEARCH_ENABLED", "on")
    cfg = gx10._apply_env(gx10._code_defaults())
    assert cfg["search"]["adapter"] == "mock"
    assert cfg["search"]["count"] == 5 and cfg["search"]["max_output_chars"] == 1234
    assert cfg["search"]["enabled"] is True


def test_web_search_keys_are_frozen():
    for k in ("security.web_in_sealed", "search.enabled", "search.adapter", "search.api_key_env"):
        assert k in gx10._FROZEN_CONFIG_KEYS                # boot-only → /config set is refused


def test_max_output_chars_config_caps_handler_output(monkeypatch):
    monkeypatch.setattr(gx10, "_WEBSEARCH", MockAdapter())
    monkeypatch.setattr(gx10, "_is_sealed_profile", lambda: False)
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", {"search": {"max_output_chars": 50}})
    out = gx10.run_tool("web_search", {"query": "latest"})
    assert out.endswith(_SOURCES_REMINDER) and len(out) <= 50 + len(_SOURCES_REMINDER) + 2


def test_count_config_flows_to_native_adapter(monkeypatch):
    monkeypatch.setenv("GX10_SEARCH_API_KEY", "k")
    a = build_web_search_adapter(
        {"search": {"enabled": True, "adapter": "brave", "count": 3}}, None,
        runner_mode="local",
    )
    assert isinstance(a, BraveAdapter) and a._count == 3


def test_api_key_value_is_never_in_config():
    # the config default holds the env-var NAME, not a secret value
    s = gx10._code_defaults()["search"]
    assert s["api_key_env"].isupper() and "=" not in s["api_key_env"]


def test_pyproject_base_deps_stay_dependency_light():
    # epic #505 R3: check_core_boundary + clean-room do not inspect pyproject base deps, so guard here
    # that the stdlib-only decision (Fork 1) holds — no HTTP client crept into the standalone wheel.
    pp = pathlib.Path(gx10.__file__).resolve().parents[1] / "pyproject.toml"
    text = pp.read_text(encoding="utf-8")
    m = re.search(r"(?ms)^\s*dependencies\s*=\s*\[(.*?)\]", text)
    assert m, "no [project] dependencies array found in core/pyproject.toml"
    deps = m.group(1).lower()
    for forbidden in ("httpx", "requests", "aiohttp", "urllib3"):
        assert forbidden not in deps, f"{forbidden} crept into the base dependencies"
    assert "pydantic" in deps
