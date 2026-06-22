"""Deterministic `/<prompt-name>` slash invocation + guided elicitation (#148), model-free.

Exercises the engine command-router resolution that runs a prompt item directly:
`_parse_prompt_args` (single-positional + key=value + --lang), `_invoke_prompt` (assemble when
required vars are present, else guide), and the `_dispatch` ordering — a real command always wins,
an unknown `/x` still falls through to a model turn. No model, no network.
"""
from __future__ import annotations

import json
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

_SINGLE = """\
---
capability: explainer
kind: prompt
description: Explain a thing
variables: [code]
required: [code]
---
Explain: {code}
"""


@pytest.fixture(autouse=True)
def _clean():
    yield
    gx10._PLUGIN_TOOLS.clear()
    gx10._PLAYBOOKS.clear()
    gx10._PROMPTS.clear()


def _load(tmp_path, md: str, cap: str, *, de_template: str | None = None) -> None:
    d = tmp_path / "skills" / cap
    (d / "locales").mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(md, encoding="utf-8")
    if de_template is not None:
        (d / "locales" / "de.json").write_text(json.dumps({"template": de_template}), encoding="utf-8")
    gx10._discover_prompts_into(str(tmp_path / "skills"))


class _FakeAgent:
    def __init__(self):
        self.turns: list[str] = []
        self.status_calls = 0

    def run(self, text):
        self.turns.append(text)

    def save_session(self):
        pass

    def status(self):
        self.status_calls += 1
        return "STATUS-SENTINEL"


def _capture(monkeypatch) -> list[str]:
    out: list[str] = []
    monkeypatch.setattr(gx10, "_ui_print", lambda *a, **k: out.append(" ".join(str(x) for x in a)))
    return out


# ── argument parsing ──────────────────────────────────────────────────────────
def test_single_positional_maps_to_the_one_required_var(tmp_path):
    _load(tmp_path, _SINGLE, "explainer")
    p = gx10._PROMPTS["explainer"]
    values, lang, err = gx10._parse_prompt_args(p, "def f(): return 1")
    assert err is None and lang is None and values == {"code": "def f(): return 1"}


def test_key_value_and_lang_parsing(tmp_path):
    _load(tmp_path, _MD, "blog-post")
    p = gx10._PROMPTS["blog-post"]
    values, lang, err = gx10._parse_prompt_args(p, 'topic=LLMs audience="end users" --lang de')
    assert err is None and lang == "de"
    assert values == {"topic": "LLMs", "audience": "end users"}


def test_positional_value_with_equals_is_preserved(tmp_path):
    # regression (review #148, MAJOR): a single-required value containing '=' (code/diff) must NOT
    # be shlex-tokenised — the whole text is the value, verbatim.
    _load(tmp_path, _SINGLE, "explainer")
    p = gx10._PROMPTS["explainer"]
    values, lang, err = gx10._parse_prompt_args(p, "def f(x=1): return x == 1")
    assert err is None and lang is None
    assert values == {"code": "def f(x=1): return x == 1"}


def test_positional_value_with_midstring_lang_is_preserved(tmp_path):
    # regression (review #148, MAJOR): '--lang' inside the value is content, not the lang option
    _load(tmp_path, _SINGLE, "explainer")
    p = gx10._PROMPTS["explainer"]
    values, lang, err = gx10._parse_prompt_args(p, "pass the --lang flag here")
    assert lang is None and values == {"code": "pass the --lang flag here"}


def test_trailing_lang_is_peeled_from_positional(tmp_path):
    _load(tmp_path, _SINGLE, "explainer")
    p = gx10._PROMPTS["explainer"]
    values, lang, err = gx10._parse_prompt_args(p, "x = y + 1 --lang de")
    assert lang == "de" and values == {"code": "x = y + 1"}


def test_explicit_kv_for_single_required_var(tmp_path):
    # when the text begins with a declared var assignment, it is key=value (quoted for spaces)
    _load(tmp_path, _SINGLE, "explainer")
    p = gx10._PROMPTS["explainer"]
    values, _, err = gx10._parse_prompt_args(p, 'code="hello world"')
    assert err is None and values == {"code": "hello world"}


# ── invocation ────────────────────────────────────────────────────────────────
def test_invoke_assembles_when_required_present(tmp_path):
    _load(tmp_path, _MD, "blog-post")
    out = gx10._invoke_prompt('blog-post topic=LLMs audience=devs tone=concise')
    assert "assembled prompt (en)" in out
    assert "Write a devs-facing post about LLMs. Tone: concise." in out


def test_invoke_guides_when_required_missing(tmp_path):
    _load(tmp_path, _MD, "blog-post")
    out = gx10._invoke_prompt("blog-post")
    assert "Required:" in out
    assert "topic" in out and "What is the post about?" in out
    assert "audience" in out
    # optional var surfaced too
    assert "tone" in out


def test_invoke_assembles_in_target_language(tmp_path):
    _load(tmp_path, _MD, "blog-post",
          de_template="Schreibe fuer {audience} ueber {topic}. Ton: {tone}.")
    out = gx10._invoke_prompt('blog-post topic=LLMs audience=Entwickler tone=knapp --lang de')
    assert "assembled prompt (de)" in out
    assert "Schreibe fuer Entwickler ueber LLMs. Ton: knapp." in out


# ── dispatch ordering / routing ─────────────────────────────────────────────────
def test_dispatch_routes_prompt_name(tmp_path, monkeypatch):
    _load(tmp_path, _MD, "blog-post")
    out = _capture(monkeypatch)
    agent = _FakeAgent()
    gx10._dispatch(agent, 'blog-post topic=X audience=Y')
    assert not agent.turns                       # NOT sent to the model
    assert any("assembled prompt" in line for line in out)


def test_real_command_wins_over_colliding_prompt_name(tmp_path, monkeypatch):
    # a prompt whose capability equals a built-in command must NOT shadow the command
    status_md = _MD.replace("capability: blog-post", "capability: status")
    _load(tmp_path, status_md, "status")
    _capture(monkeypatch)
    agent = _FakeAgent()
    gx10._dispatch(agent, "status")
    assert agent.status_calls == 1 and not agent.turns


def test_unknown_slash_still_falls_through_to_a_turn(tmp_path, monkeypatch):
    _load(tmp_path, _MD, "blog-post")
    _capture(monkeypatch)
    agent = _FakeAgent()
    gx10._dispatch(agent, "totally-unknown-thing please")
    assert agent.turns == ["totally-unknown-thing please"]   # routed to the model, not a prompt


def test_dispatch_whitespace_only_does_not_crash(tmp_path, monkeypatch):
    # regression (review #148, MINOR): the prompt guard must not IndexError on blank input
    _load(tmp_path, _MD, "blog-post")
    _capture(monkeypatch)
    agent = _FakeAgent()
    gx10._dispatch(agent, "   ")          # must not raise; routes to the model (no-op turn)
    assert agent.turns == ["   "]


def test_resolution_is_case_insensitive(tmp_path):
    _load(tmp_path, _MD, "blog-post")
    assert gx10._resolve_prompt_name("BLOG-POST topic=X") == "blog-post"
    assert gx10._resolve_prompt_name("   ") is None        # blank → None, never IndexError
    out = gx10._invoke_prompt("Blog-Post topic=LLMs audience=devs tone=t")
    assert "assembled prompt" in out
