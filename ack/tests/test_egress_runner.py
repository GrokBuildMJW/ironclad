from __future__ import annotations

import subprocess
import sys

from engine import egress_runner


def test_discover_tests_dir_returns_pytest(tmp_path):
    (tmp_path / "tests").mkdir()
    assert egress_runner.discover_build_test(tmp_path) == [[sys.executable, "-m", "pytest", "-q"]]


def test_discover_pyproject_adds_build_only_when_importable(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("[build-system]\n", encoding="utf-8")
    monkeypatch.setattr(egress_runner.importlib.util, "find_spec", lambda name: object() if name == "build" else None)
    assert egress_runner.discover_build_test(tmp_path) == [[sys.executable, "-m", "build", "--wheel"]]

    monkeypatch.setattr(egress_runner.importlib.util, "find_spec", lambda name: None)
    assert egress_runner.discover_build_test(tmp_path) == []


def test_discover_empty_tree_returns_empty(tmp_path):
    assert egress_runner.discover_build_test(tmp_path) == []


def test_run_hermetic_network_none_no_backend_is_advisory_skip(tmp_path, monkeypatch):
    monkeypatch.setattr(egress_runner.sandbox, "available_backend", lambda pref: "")
    monkeypatch.setattr(egress_runner, "discover_build_test", lambda root: [[sys.executable, "-m", "pytest", "-q"]])
    out = egress_runner.run_hermetic(tmp_path, network="none")
    assert out["ran"] is False
    assert out["contained"] is False
    assert out["backend"] == ""
    assert out["findings"] == [{
        "command": "",
        "reason": "no sandbox backend - egress containment not enforced here",
        "severity": "advisory",
    }]


def test_run_hermetic_network_none_nonzero_blocks(tmp_path, monkeypatch):
    monkeypatch.setattr(egress_runner.sandbox, "available_backend", lambda pref: "bwrap")
    monkeypatch.setattr(egress_runner, "discover_build_test", lambda root: [["python", "-m", "pytest", "-q"]])
    wrapped = []

    def _wrap(command, *, backend, net):
        wrapped.append((command, backend, net))
        return "wrapped " + command

    class _CP:
        returncode = 7
        stdout = "out"
        stderr = "err"

    monkeypatch.setattr(egress_runner.sandbox, "wrap_command", _wrap)
    monkeypatch.setattr(egress_runner, "_run_command", lambda *a, **k: _CP())
    out = egress_runner.run_hermetic(tmp_path, network="none")
    assert out["ran"] is True
    assert out["contained"] is True
    assert out["backend"] == "bwrap"
    assert wrapped == [("python -m pytest -q", "bwrap", False)]
    assert out["findings"] == [{
        "command": "python -m pytest -q",
        "reason": "build/test failed under denied network",
        "severity": "block",
        "exit_code": 7,
        "output_tail": "out\nerr",
    }]


def test_run_hermetic_network_none_zero_has_no_finding(tmp_path, monkeypatch):
    monkeypatch.setattr(egress_runner.sandbox, "available_backend", lambda pref: "bwrap")
    monkeypatch.setattr(egress_runner, "discover_build_test", lambda root: [["python", "-m", "pytest", "-q"]])

    class _CP:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(egress_runner, "_run_command", lambda *a, **k: _CP())
    out = egress_runner.run_hermetic(tmp_path, network="none")
    assert out["ran"] is True
    assert out["contained"] is True
    assert out["findings"] == []


def test_run_hermetic_declared_nonzero_is_advisory(tmp_path, monkeypatch):
    monkeypatch.setattr(egress_runner.sandbox, "available_backend", lambda pref: "bwrap")
    monkeypatch.setattr(egress_runner, "discover_build_test", lambda root: [["python", "-m", "pytest", "-q"]])

    class _CP:
        returncode = 1
        stdout = "needs network"
        stderr = ""

    monkeypatch.setattr(egress_runner, "_run_command", lambda *a, **k: _CP())
    out = egress_runner.run_hermetic(tmp_path, network="declared")
    assert out["findings"][0]["severity"] == "advisory"
    assert out["findings"][0]["reason"] == "build/test failed in sandbox"


def test_run_hermetic_runner_error_is_advisory(tmp_path, monkeypatch):
    monkeypatch.setattr(egress_runner.sandbox, "available_backend", lambda pref: "bwrap")
    monkeypatch.setattr(egress_runner, "discover_build_test", lambda root: [["python", "-m", "pytest", "-q"]])

    def _boom(*a, **k):
        raise OSError("spawn failed")

    monkeypatch.setattr(egress_runner, "_run_command", _boom)
    out = egress_runner.run_hermetic(tmp_path, network="none")
    assert out["findings"][0]["severity"] == "advisory"
    assert "spawn failed" in out["findings"][0]["reason"]


def test_run_hermetic_timeout_is_advisory(tmp_path, monkeypatch):
    monkeypatch.setattr(egress_runner.sandbox, "available_backend", lambda pref: "bwrap")
    monkeypatch.setattr(egress_runner, "discover_build_test", lambda root: [["python", "-m", "pytest", "-q"]])

    def _timeout(*a, **k):
        raise subprocess.TimeoutExpired("cmd", 1, output="partial", stderr="late")

    monkeypatch.setattr(egress_runner, "_run_command", _timeout)
    out = egress_runner.run_hermetic(tmp_path, network="none")
    assert out["findings"][0]["severity"] == "advisory"
    assert out["findings"][0]["output_tail"] == "partial\nlate"
