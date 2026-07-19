"""Hermetic sibling-test runner (S11b-2): gate.run_sibling_test_hermetic and gate.gate_generated execute path."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from ack import gate
import pytest


def _mk(base, testbody: str, *, sentinel: bool = False):
    (base / 'skills').mkdir(parents=True, exist_ok=True)
    (base / 'tests').mkdir(parents=True, exist_ok=True)
    skill = ['CASE = {"capability": "cap-x", "name": "tool_x", "description": "d"}', 'def run(context: dict | None = None) -> dict:']
    if sentinel:
        skill.append('    # ' + gate.SCAFFOLD_SENTINEL)
    skill.append('    return {"ok": True}')
    py = base / 'skills' / 'foo.py'
    py.write_text(chr(10).join(skill) + chr(10), encoding='utf-8')
    (base / 'tests' / 'test_foo.py').write_text(testbody, encoding='utf-8')
    return py


def test_hermetic_passing_test(tmp_path):
    py = _mk(tmp_path, 'def test_ok():\n    assert 1 + 1 == 2\n')
    ok, detail = gate.run_sibling_test_hermetic(py)
    assert ok, detail


def test_hermetic_child_uses_plain_assertions(tmp_path):
    py = _mk(tmp_path, 'def test_ok():\n    assert True\n')
    completed = SimpleNamespace(returncode=0, stdout="", stderr="")

    with patch.object(gate.subprocess, "run", return_value=completed) as run:
        ok, detail = gate.run_sibling_test_hermetic(py)

    assert ok, detail
    assert "--assert=plain" in run.call_args.args[0]


def test_hermetic_failing_test(tmp_path):
    py = _mk(tmp_path, 'def test_bad():\n    assert False\n')
    ok, detail = gate.run_sibling_test_hermetic(py)
    assert not ok and 'fail' in detail.lower()


def test_hermetic_scrubs_credential_env(monkeypatch, tmp_path):
    monkeypatch.setenv('GH_TOKEN', 'supersecret')
    py = _mk(tmp_path, 'import os\ndef test_scrubbed():\n    assert os.environ.get("GH_TOKEN") is None\n')
    ok, detail = gate.run_sibling_test_hermetic(py)
    assert ok, detail


def test_hermetic_hard_timeout(tmp_path):
    py = _mk(tmp_path, 'import time\ndef test_hang():\n    time.sleep(30)\n')
    ok, detail = gate.run_sibling_test_hermetic(py, timeout=3)
    assert not ok and 'timeout' in detail.lower()


def test_hermetic_missing_sibling_test(tmp_path):
    (tmp_path / 'skills').mkdir(parents=True, exist_ok=True)
    py = tmp_path / 'skills' / 'foo.py'
    py.write_text('x = 1\n', encoding='utf-8')
    ok, detail = gate.run_sibling_test_hermetic(py)
    assert not ok and 'no sibling test' in detail.lower()


def test_gate_generated_execute_runs_the_test(tmp_path):
    py = _mk(tmp_path, 'def test_ok():\n    assert True\n', sentinel=False)
    assert gate.gate_generated(py, execute=True).passed


def test_gate_generated_execute_fails_on_red_test(tmp_path):
    py = _mk(tmp_path, 'def test_bad():\n    assert False\n', sentinel=False)
    r = gate.gate_generated(py, execute=True)
    assert not r.passed and any('hermetic' in x.lower() for x in r.reasons)


def test_gate_generated_default_does_not_execute(tmp_path):
    py = _mk(tmp_path, 'def test_bad():\n    assert False\n', sentinel=False)
    assert gate.gate_generated(py).passed


def test_hermetic_accepts_relative_path(tmp_path, monkeypatch):
    # a RELATIVE py_path must still resolve: pytest runs with cwd=tmp, so the test path is resolved up front
    _mk(tmp_path, 'def test_ok():\n    assert True\n')
    monkeypatch.chdir(tmp_path)
    ok, detail = gate.run_sibling_test_hermetic("skills/foo.py")
    assert ok, detail
