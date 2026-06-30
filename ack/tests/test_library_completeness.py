from __future__ import annotations

from ack import gate
import pytest


def _lib_item(libroot, domain, name, *, sentinel, testbody='def test_x():\n    assert True\n'):
    skills = libroot / domain / 'skills'
    skills.mkdir(parents=True, exist_ok=True)
    tests = libroot / domain / 'tests'
    tests.mkdir(parents=True, exist_ok=True)
    lines = [
        'CASE = {"capability": "' + name + '-cap", "name": "' + name + '", "description": "d"}',
        'def run(context: dict | None = None) -> dict:',
    ]
    if sentinel:
        lines.append('    # ' + gate.SCAFFOLD_SENTINEL)
    lines.append('    return {"ok": True}')
    (skills / (name + '.py')).write_text(chr(10).join(lines) + chr(10), encoding='utf-8')
    (tests / ('test_' + name + '.py')).write_text(testbody, encoding='utf-8')


def test_missing_root_is_empty(tmp_path):
    assert gate.library_items_complete(tmp_path / 'nope') == []


def test_flags_unfilled_scaffold(tmp_path):
    _lib_item(tmp_path, 'Widgets', 'sprocket', sentinel=True)
    probs = gate.library_items_complete(tmp_path)
    assert len(probs) == 1 and 'sprocket' in probs[0] and 'scaffold' in probs[0].lower()


def test_filled_library_is_clean(tmp_path):
    _lib_item(tmp_path, 'Widgets', 'sprocket', sentinel=False)
    assert gate.library_items_complete(tmp_path) == []


def test_execute_flags_failing_sibling_test(tmp_path):
    _lib_item(
        tmp_path,
        'Widgets',
        'sprocket',
        sentinel=False,
        testbody='def test_bad():\n    assert False\n',
    )
    assert gate.library_items_complete(tmp_path) == []
    probs = gate.library_items_complete(tmp_path, execute=True)
    assert probs and any('hermetic' in p.lower() for p in probs)


def test_flags_multiple_scaffolds(tmp_path):
    _lib_item(tmp_path, 'A', 'one', sentinel=True)
    _lib_item(tmp_path, 'B', 'two', sentinel=True)
    assert len(gate.library_items_complete(tmp_path)) == 2

def _lib_prompt(libroot, domain, name, *, de=True):
    d = libroot / domain / name
    (d / 'locales').mkdir(parents=True, exist_ok=True)
    (d / 'SKILL.md').write_text(
        '---\ncapability: ' + name + '-cap\nkind: prompt\ndescription: d\n'
        'languages: [en, de]\nvariables: [input]\nrequired: [input]\n---\n'
        'Use {input}.\n',
        encoding='utf-8')
    if de:
        (d / 'locales' / 'de.json').write_text(
            '{"template": "Nutze {input}."}', encoding='utf-8')
    return d / 'SKILL.md'


def test_library_complete_flags_prompt_missing_declared_overlay(tmp_path):
    _lib_prompt(tmp_path, 'Writing', 'brief', de=False)
    probs = gate.library_items_complete(tmp_path)
    assert len(probs) == 1 and 'brief' in probs[0] and 'overlay' in probs[0].lower()


def test_library_complete_passes_complete_prompt(tmp_path):
    _lib_prompt(tmp_path, 'Writing', 'brief', de=True)
    assert gate.library_items_complete(tmp_path) == []


def test_library_complete_passes_generated_prompt(tmp_path):
    from ack import generator as g
    args = g.build_parser().parse_args([
        '--kind', 'prompt',
        '--domain', 'writing',
        '--case', 'blog-brief',
        '--description', 'x',
    ])
    g.generate(g.build_context(args), template_root=g.template_root_for(args), output_root=tmp_path)
    assert gate.library_items_complete(tmp_path) == []
