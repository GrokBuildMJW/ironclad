"""Prompt-library item format (`kind: prompt`, #108) — parse/validate/discover + variable build.
Reuses the shared ack.playbook frontmatter parser; flat variable encoding (no YAML-block surgery).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ack import prompt as P


_PROMPT_MD = """\
---
capability: blog-post
kind: prompt
description: Draft a blog post brief
type: prompt
domain: writing
languages: [en, de]
variables: [topic, audience]
required: [topic]
ask.topic: What is the topic?
ask.audience: Who is the audience?
desc.topic: The subject of the post
version: "0.1.0"
provenance: built-in
---
Write a {audience}-facing post about {topic}.
"""


def _write(root: Path, cap: str = "blog-post", body: str = _PROMPT_MD) -> Path:
    d = root / "skills" / cap
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(body, encoding="utf-8")
    return d / "SKILL.md"


def test_parse_prompt_meta_and_template(tmp_path):
    p = P.parse_prompt(_write(tmp_path))
    assert p.capability == "blog-post" and p.description == "Draft a blog post brief"
    assert p.languages == ["en", "de"]
    assert "Write a {audience}-facing post about {topic}." in p.template
    md = p.metadata()
    assert md["kind"] == "prompt" and md["variables"] == ["topic", "audience"]


def test_variables_built_with_required_desc_question(tmp_path):
    p = P.parse_prompt(_write(tmp_path))
    by = {v.name: v for v in p.variables}
    assert by["topic"].required is True and by["audience"].required is False   # only topic in `required`
    assert by["topic"].description == "The subject of the post"
    assert by["topic"].question == "What is the topic?"
    assert by["audience"].question == "Who is the audience?"


def test_all_required_when_no_required_key(tmp_path):
    body = _PROMPT_MD.replace("required: [topic]\n", "")
    p = P.parse_prompt(_write(tmp_path, "no-req", body))
    assert all(v.required for v in p.variables)   # absent `required` → all required


def test_validate_rejects_bad_kind_and_nonlist(tmp_path):
    errs = P.validate_prompt_meta({"capability": "x", "description": "d", "kind": "playbook"})
    assert any("kind must be" in e for e in errs)
    errs2 = P.validate_prompt_meta({"capability": "x", "description": "d", "kind": "prompt",
                                    "variables": "nope"})
    assert any("variables" in e and "list" in e for e in errs2)


def test_missing_required_field_raises(tmp_path):
    bad = "---\nkind: prompt\ndescription: no capability\n---\nbody\n"
    with pytest.raises(P.PromptError):
        P.parse_prompt(_write(tmp_path, "bad", bad))


def test_discover_only_prompt_items(tmp_path):
    _write(tmp_path, "blog-post")
    _write(tmp_path, "tweet", _PROMPT_MD.replace("blog-post", "tweet"))
    # a playbook (kind: playbook) in the same tree must be ignored by discover_prompts
    pb = tmp_path / "skills" / "a-playbook"
    pb.mkdir(parents=True, exist_ok=True)
    (pb / "SKILL.md").write_text("---\ncapability: a-playbook\nkind: playbook\ndescription: d\n---\nx\n",
                                 encoding="utf-8")
    found = {p.capability for p in P.discover_prompts(tmp_path)}
    assert found == {"blog-post", "tweet"}
    assert not P.is_prompt_item(pb / "SKILL.md")


def test_discover_missing_root_empty():
    assert P.discover_prompts("/no/such/dir") == []
