"""Reply-language setting (GX10_LANGUAGE / generation.language).

OSS default is English; an operator can pin another language (e.g. German). The
directive is injected into the orchestrator's system prompt at construction.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=lambda **kw: object()))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402
import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _restore():
    lang = gx10.LANGUAGE
    yield
    gx10.LANGUAGE = lang


def test_guidance_names_language():
    assert "German" in gx10._language_guidance("de")
    assert "English" in gx10._language_guidance("en")
    assert "French" in gx10._language_guidance("fr")


def test_default_is_english():
    cfg = gx10._code_defaults()
    assert cfg["generation"]["language"] == "en"


def test_apply_config_sets_language():
    cfg = gx10._code_defaults()
    cfg["generation"]["language"] = "de"
    gx10._apply_config(cfg)
    assert gx10.LANGUAGE == "de"


def test_env_override(monkeypatch):
    monkeypatch.setenv("GX10_LANGUAGE", "fr")
    cfg = gx10._apply_env(gx10._code_defaults())
    assert cfg["generation"]["language"] == "fr"


def test_directive_lands_in_system_prompt(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # the openai stub may be shared from another test (OpenAI=object); force a
    # constructible client for this one.
    monkeypatch.setattr(gx10, "OpenAI", lambda **kw: object())
    gx10.LANGUAGE = "de"
    agent = gx10.GX10(base_url="http://x/v1", api_key="k", model="m", prompt_path="")
    sys_msgs = [m for m in agent.messages if m.get("role") == "system"]
    assert sys_msgs and any("German" in m["content"] for m in sys_msgs)
