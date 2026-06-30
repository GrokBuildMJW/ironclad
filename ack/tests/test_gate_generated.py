"""Generation-completeness gate (S11b-1): a generated skill must pass the doctor preflight
*and* no longer carry the unfilled-scaffold sentinel.
"""
from __future__ import annotations

from ack import gate
import pytest


def _make_skill(base, *, sentinel: bool, with_test: bool = True):
    skills = base / 'skills'
    skills.mkdir(parents=True, exist_ok=True)
    lines = [
        'CASE = {"capability": "cap-x", "name": "tool_x", "description": "d"}',
        '',
        'def run(context: dict | None = None) -> dict:',
    ]
    if sentinel:
        lines.append('    # ' + gate.SCAFFOLD_SENTINEL)
    lines.append('    return {"ok": True}')
    py = skills / 'foo.py'
    py.write_text(chr(10).join(lines) + chr(10), encoding='utf-8')
    if with_test:
        tests = base / 'tests'
        tests.mkdir(parents=True, exist_ok=True)
        (tests / 'test_foo.py').write_text('def test_x():\n    assert True\n', encoding='utf-8')
    return py


def test_has_scaffold_sentinel_detects_marker(tmp_path):
    py = _make_skill(tmp_path, sentinel=True)
    assert gate.has_scaffold_sentinel(py) is True
    py2 = _make_skill(tmp_path / 'b', sentinel=False)
    assert gate.has_scaffold_sentinel(py2) is False


def test_has_scaffold_sentinel_missing_file_is_false(tmp_path):
    assert gate.has_scaffold_sentinel(tmp_path / 'nope.py') is False


def test_gate_generated_rejects_unfilled_scaffold(tmp_path):
    py = _make_skill(tmp_path, sentinel=True)
    r = gate.gate_generated(py)
    assert not r.passed and r.kind == 'generated' and any('scaffold' in x.lower() for x in r.reasons)


def test_gate_generated_passes_filled(tmp_path):
    py = _make_skill(tmp_path, sentinel=False)
    r = gate.gate_generated(py)
    assert r.passed, r.reasons


def test_gate_generated_is_stricter_than_gate_tool(tmp_path):
    py = _make_skill(tmp_path, sentinel=True)
    assert gate.gate_tool(py).passed is True
    assert gate.gate_generated(py).passed is False


def test_gate_generated_inherits_missing_test_failure(tmp_path):
    py = _make_skill(tmp_path, sentinel=False, with_test=False)
    r = gate.gate_generated(py)
    assert not r.passed and any('test' in x.lower() for x in r.reasons)
