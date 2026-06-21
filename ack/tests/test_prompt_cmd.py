"""Prompt-library slash surface + guided elicitation (ADR-0003 #110), model-free.

Exercises the engine dispatch ``_use_prompt`` end to end: a discovered ``kind: prompt`` item is
exposed as the ``use_prompt`` tool, listing works with no capability, the elicitation loop returns
the **next** required question until all required values are present, then assembles the finished
prompt in the requested language. No model, no network — pure dispatch.
"""
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


_MD = """\
---
capability: blog-post
kind: prompt
description: Draft a blog post brief
languages: [en, de]
variables: [topic, audience, tone]
required: [topic, audience]
ask.topic: What is the post about?
ask.audience: Who is the audience?
---
Write a {audience}-facing post about {topic}. Tone: {tone}.
"""


@pytest.fixture(autouse=True)
def _clean():
    yield
    gx10._PLUGIN_TOOLS.clear()
    gx10._PLAYBOOKS.clear()
    gx10._PROMPTS.clear()


def _load(tmp_path, *, de_template: str | None = None) -> None:
    d = tmp_path / "skills" / "blog-post"
    (d / "locales").mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(_MD, encoding="utf-8")
    if de_template is not None:
        import json
        (d / "locales" / "de.json").write_text(json.dumps({"template": de_template}), encoding="utf-8")
    gx10._discover_prompts_into(str(tmp_path / "skills"))


def test_dropping_md_makes_prompt_available_as_tool(tmp_path):
    _load(tmp_path)
    assert "blog-post" in gx10._PROMPTS
    names = {t["function"]["name"] for t in gx10._effective_tools()}
    assert "use_prompt" in names                     # the prompt surface is now exposed


def test_no_prompt_means_no_tool():
    assert not gx10._PROMPTS
    names = {t["function"]["name"] for t in gx10._effective_tools()}
    assert "use_prompt" not in names                 # absent until a prompt is discovered


def test_list_with_no_capability(tmp_path):
    _load(tmp_path)
    out = gx10._use_prompt("")
    assert "blog-post" in out and "Draft a blog post brief" in out


def test_unknown_capability_errors(tmp_path):
    _load(tmp_path)
    out = gx10._use_prompt("nope")
    assert out.startswith("ERROR") and "nope" in out


def test_elicitation_asks_next_required_in_order(tmp_path):
    _load(tmp_path)
    # nothing collected → first required ('topic') with its custom question
    out = gx10._use_prompt("blog-post", "{}")
    assert "NEXT QUESTION" in out and "topic" in out and "What is the post about?" in out
    # topic given → next required ('audience')
    out2 = gx10._use_prompt("blog-post", '{"topic": "LLMs"}')
    assert "audience" in out2 and "Who is the audience?" in out2


def test_assembles_when_all_required_present(tmp_path):
    _load(tmp_path)
    out = gx10._use_prompt("blog-post", '{"topic": "LLMs", "audience": "developer", "tone": "concise"}')
    assert "ASSEMBLED PROMPT (en)" in out
    assert "Write a developer-facing post about LLMs. Tone: concise." in out


def test_assembles_in_target_language(tmp_path):
    _load(tmp_path, de_template="Schreibe fuer {audience} ueber {topic}. Ton: {tone}.")
    out = gx10._use_prompt("blog-post", '{"topic": "LLMs", "audience": "Entwickler", "tone": "knapp"}', "de")
    assert "ASSEMBLED PROMPT (de)" in out
    assert "Schreibe fuer Entwickler ueber LLMs. Ton: knapp." in out


def test_bad_values_json_is_fail_soft(tmp_path):
    _load(tmp_path)
    out = gx10._use_prompt("blog-post", "{not json")
    assert out.startswith("ERROR") and "JSON" in out


def test_values_must_be_object(tmp_path):
    _load(tmp_path)
    out = gx10._use_prompt("blog-post", "[1, 2]")
    assert out.startswith("ERROR") and "object" in out
