"""Multilingual prompt assembly (#109) — assemble in EN + DE, missing-required, fallback."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ack import prompt as P
from ack import promptgen as G


_MD = """\
---
capability: blog-post
kind: prompt
description: Draft a blog post brief
languages: [en, de]
variables: [topic, audience, tone]
required: [topic]
---
Write a {audience}-facing post about {topic}. Tone: {tone}.
"""


def _item(tmp_path, *, de_template: str | None = None) -> P.Prompt:
    d = tmp_path / "skills" / "blog-post"
    (d / "locales").mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(_MD, encoding="utf-8")
    if de_template is not None:
        (d / "locales" / "de.json").write_text(json.dumps({"template": de_template}), encoding="utf-8")
    return P.parse_prompt(d / "SKILL.md")


def test_assemble_source_language(tmp_path):
    p = _item(tmp_path)
    out = G.assemble(p, {"topic": "LLMs", "audience": "developer", "tone": "concise"})
    assert out == "Write a developer-facing post about LLMs. Tone: concise."


def test_assemble_target_language_overlay(tmp_path):
    p = _item(tmp_path, de_template="Schreibe einen Beitrag fuer {audience} ueber {topic}. Ton: {tone}.")
    out = G.assemble(p, {"topic": "LLMs", "audience": "Entwickler", "tone": "knapp"}, lang="de")
    assert out == "Schreibe einen Beitrag fuer Entwickler ueber LLMs. Ton: knapp."


def test_missing_overlay_falls_back_to_source(tmp_path):
    p = _item(tmp_path)  # no de.json
    out = G.assemble(p, {"topic": "X", "audience": "Y", "tone": "Z"}, lang="de")
    assert out.startswith("Write a Y-facing post about X")   # source-language fallback


def test_missing_required_raises_and_is_listed(tmp_path):
    p = _item(tmp_path)
    assert G.missing_required(p, {"audience": "dev"}) == ["topic"]
    with pytest.raises(G.AssemblyError):
        G.assemble(p, {"audience": "dev"})   # 'topic' required, absent


def test_optional_unset_becomes_empty(tmp_path):
    p = _item(tmp_path)  # tone/audience optional (only topic required)
    out = G.assemble(p, {"topic": "X"})
    assert out == "Write a -facing post about X. Tone: ."   # optional placeholders → empty


def test_undeclared_placeholder_left_verbatim(tmp_path):
    d = tmp_path / "skills" / "p"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\ncapability: p\nkind: prompt\ndescription: d\nvariables: [a]\nrequired: [a]\n---\n{a} and {undeclared}\n",
        encoding="utf-8")
    p = P.parse_prompt(d / "SKILL.md")
    assert G.assemble(p, {"a": "X"}) == "X and {undeclared}"
