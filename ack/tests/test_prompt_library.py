"""Curated multilingual starter prompt library (#111).

Asserts the prompts that **ship** under ``skills/prompts/`` are real, discoverable, pass the
registration/eval gate, and assemble in every language they declare (EN + DE) — proving the
end-to-end claim **"a new prompt = drop an MD file, no engine code change"**. Model-free.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ack import gate
from ack import prompt as P
from ack import promptgen as G

_PROMPTS_DIR = Path(__file__).resolve().parents[2] / "skills" / "prompts"

# The curated set this library guarantees. Each must round-trip through the gate in EN + DE.
_CURATED = {"code-review", "commit-message", "bug-report", "explain-code"}


def _discovered() -> dict[str, P.Prompt]:
    return {p.capability: p for p in P.discover_prompts(_PROMPTS_DIR)}


def test_curated_set_is_discoverable():
    found = _discovered()
    missing = _CURATED - set(found)
    assert not missing, f"curated prompts not discovered: {missing}"


@pytest.mark.parametrize("cap", sorted(_CURATED))
def test_each_curated_prompt_passes_the_gate(cap):
    p = _discovered()[cap]
    res = gate.gate_prompt(p.path)
    assert res.passed, res.reasons


@pytest.mark.parametrize("cap", sorted(_CURATED))
def test_each_curated_prompt_declares_en_and_de(cap):
    p = _discovered()[cap]
    assert "en" in p.languages and "de" in p.languages


@pytest.mark.parametrize("cap", sorted(_CURATED))
def test_each_curated_prompt_assembles_in_en_and_de(cap):
    p = _discovered()[cap]
    sample = {v.name: f"<{v.name}>" for v in p.variables}
    en = G.assemble(p, sample, lang="en")
    de = G.assemble(p, sample, lang="de")
    assert en.strip() and de.strip()
    assert de != en                      # the DE overlay actually translated the template
    for v in p.variables:                # every provided value is substituted, no leftover braces
        assert f"{{{v.name}}}" not in en and f"{{{v.name}}}" not in de


def test_required_variables_are_used_in_every_template():
    # the gate enforces this, but assert it directly on the shipped set as a guard
    for cap, p in _discovered().items():
        if cap not in _CURATED:
            continue
        used = set(G._PLACEHOLDER.findall(p.template))
        for v in p.variables:
            if v.required:
                assert v.name in used, f"{cap}: required {v.name!r} unused in template"


def test_dropping_a_new_md_adds_a_prompt(tmp_path):
    # the headline claim — a brand-new MD under a skills root is discovered with no code change
    d = tmp_path / "skills" / "tagline"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\ncapability: tagline\nkind: prompt\ndescription: Write a tagline\n"
        "variables: [product]\nrequired: [product]\n---\nWrite a tagline for {product}.\n",
        encoding="utf-8")
    found = {p.capability for p in P.discover_prompts(tmp_path / "skills")}
    assert "tagline" in found
    assert gate.gate(d / "SKILL.md").kind == "prompt"
