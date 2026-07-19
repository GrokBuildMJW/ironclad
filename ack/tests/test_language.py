"""Reply-language setting (GX10_LANGUAGE / generation.language).

OSS default is English; an operator can pin another language (e.g. German). The
directive is injected at construction and replaced after a live language change.
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
def _restore(monkeypatch):
    lang = gx10.LANGUAGE
    monkeypatch.setattr(gx10, "_SESSION_OVERRIDES", {})
    yield
    gx10.LANGUAGE = lang


def _live_agent(monkeypatch, language="en"):
    cfg = gx10._code_defaults()
    cfg["generation"]["language"] = language
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", cfg)
    monkeypatch.setattr(gx10, "LANGUAGE", language)
    monkeypatch.setattr(gx10, "_UI_APP", None, raising=False)
    monkeypatch.setattr(gx10, "_UI_SINK", lambda _line: None)
    agent = object.__new__(gx10.GX10)
    agent.messages = [{"role": "system", "content": "Base prompt"}]
    agent._append_guidance(gx10._language_guidance(language))
    return agent


def _system_content(agent):
    return next(m["content"] for m in agent.messages if m.get("role") == "system")


def _language_block_count(agent):
    content = _system_content(agent)
    return content.count(gx10._LANGUAGE_GUIDANCE_MARKERS[0])


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
    assert _language_block_count(agent) == 1


def test_runtime_language_set_rebuilds_live_system_prompt(monkeypatch):
    agent = _live_agent(monkeypatch)

    gx10._dispatch(agent, "config set generation.language de")

    content = _system_content(agent)
    assert "Always respond to the user in German" in content
    assert "Always respond to the user in English" not in content
    assert _language_block_count(agent) == 1


def test_runtime_language_switches_never_stack_blocks(monkeypatch):
    agent = _live_agent(monkeypatch)

    for language, name in (("de", "German"), ("en", "English"), ("de", "German")):
        gx10._dispatch(agent, f"config set generation.language {language}")
        assert f"Always respond to the user in {name}" in _system_content(agent)
        assert _language_block_count(agent) == 1


def test_runtime_language_same_value_is_byte_identical(monkeypatch):
    agent = _live_agent(monkeypatch, "de")
    before = _system_content(agent).encode("utf-8")

    gx10._dispatch(agent, "config set generation.language de")

    assert _system_content(agent).encode("utf-8") == before


def test_rebuilt_language_survives_session_save_load(monkeypatch, tmp_path):
    agent = _live_agent(monkeypatch)
    session_file = tmp_path / "session.json"
    monkeypatch.setattr(gx10, "session_path", lambda: session_file)
    gx10._dispatch(agent, "config set generation.language de")
    agent.messages.append({"role": "user", "content": "saved turn"})
    rebuilt_system = _system_content(agent)
    agent.save_session(strict=True)

    agent.messages = [{"role": "system", "content": rebuilt_system}]
    assert agent.load_session() == 1
    assert _system_content(agent) == rebuilt_system
    assert _language_block_count(agent) == 1


def test_runtime_language_rebuild_is_fail_soft_without_live_system_prompt(monkeypatch):
    agent_without_system = object.__new__(gx10.GX10)
    agent_without_system.messages = []
    for agent in (types.SimpleNamespace(), agent_without_system):
        cfg = gx10._code_defaults()
        cfg["generation"]["language"] = "en"
        monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", cfg)
        monkeypatch.setattr(gx10, "LANGUAGE", "en")
        lines = []
        monkeypatch.setattr(gx10, "_UI_APP", None, raising=False)
        monkeypatch.setattr(gx10, "_UI_SINK", lambda line: lines.append(line))

        gx10._dispatch(agent, "config set generation.language de")

        assert gx10.LANGUAGE == "de"
        assert any("[config] set generation.language = 'de'" in line for line in lines)
