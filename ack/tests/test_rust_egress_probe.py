from __future__ import annotations

import os
import subprocess

import pytest

from engine import egress_runner


class _CP:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _cargo_project(root):
    (root / "Cargo.toml").write_text("[package]\nname = \"demo\"\nversion = \"0.1.0\"\n", encoding="utf-8")


def _setup(monkeypatch, results):
    calls = []

    monkeypatch.setattr(egress_runner.shutil, "which", lambda name: "cargo" if name == "cargo" else None)
    monkeypatch.setattr(egress_runner.sandbox, "available_backend", lambda pref: "bwrap")
    monkeypatch.setattr(egress_runner.sandbox, "wrap_command",
                        lambda command, *, backend, net: f"wrapped {command}")

    def _run(command, project_root, env, timeout):
        calls.append((command, project_root, env, timeout))
        result = results[len(calls) - 1]
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(egress_runner, "_run_cargo", _run)
    return calls


def test_network_none_net_deny_signature_blocks(tmp_path, monkeypatch):
    _cargo_project(tmp_path)
    calls = _setup(monkeypatch, [
        _CP(),
        _CP(1, stderr="build.rs: connect failed: Network is unreachable"),
        _CP(),
    ])

    out = egress_runner.run_rust_hermetic(tmp_path, network="none")

    assert out["ran"] is True
    assert out["contained"] is True
    assert out["backend"] == "bwrap"
    assert [call[0] for call in calls] == [
        "cargo fetch --locked",
        "wrapped cargo build --frozen --offline",
        "wrapped cargo test --no-run --frozen --offline",
    ]
    assert out["findings"][0]["severity"] == "block"
    assert out["findings"][0]["ecosystem"] == "rust"
    assert "rust build-time egress attempt blocked under --net=none" in out["findings"][0]["reason"]


def test_network_declared_net_deny_signature_is_advisory(tmp_path, monkeypatch):
    _cargo_project(tmp_path)
    _setup(monkeypatch, [
        _CP(),
        _CP(1, stderr="Could not resolve host: example.com"),
        _CP(),
    ])

    out = egress_runner.run_rust_hermetic(tmp_path, network="declared")

    assert out["findings"][0]["severity"] == "advisory"
    assert "rust build-time egress attempt blocked under --net=none" in out["findings"][0]["reason"]


def test_plain_compile_error_is_advisory(tmp_path, monkeypatch):
    _cargo_project(tmp_path)
    _setup(monkeypatch, [
        _CP(),
        _CP(101, stderr="error[E0425]: cannot find value `x` in this scope"),
        _CP(),
    ])

    out = egress_runner.run_rust_hermetic(tmp_path, network="none")

    assert out["findings"][0]["severity"] == "advisory"
    assert out["findings"][0]["reason"] == "rust hermetic probe inconclusive (build failed, no egress signature)"


def test_phase_two_success_has_no_finding(tmp_path, monkeypatch):
    _cargo_project(tmp_path)
    _setup(monkeypatch, [_CP(), _CP(), _CP()])

    out = egress_runner.run_rust_hermetic(tmp_path, network="none")

    assert out["findings"] == []


def test_fetch_failure_is_advisory_not_block(tmp_path, monkeypatch):
    _cargo_project(tmp_path)
    calls = _setup(monkeypatch, [_CP(101, stderr="package not found")])

    out = egress_runner.run_rust_hermetic(tmp_path, network="none")

    assert len(calls) == 1
    assert out["ran"] is False
    assert out["findings"][0]["severity"] == "advisory"
    assert out["findings"][0]["reason"] == "rust deps not fetchable - hermetic probe inconclusive"


def test_no_cargo_toml_returns_unrun_without_findings(tmp_path, monkeypatch):
    monkeypatch.setattr(egress_runner, "_run_cargo", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))

    out = egress_runner.run_rust_hermetic(tmp_path, network="none")

    assert out["ran"] is False
    assert out["findings"] == []


def test_cargo_not_on_path_is_advisory(tmp_path, monkeypatch):
    _cargo_project(tmp_path)
    monkeypatch.setattr(egress_runner.shutil, "which", lambda name: None)

    out = egress_runner.run_rust_hermetic(tmp_path, network="none")

    assert out["findings"] == [{
        "command": "",
        "reason": "cargo not available - rust hermetic probe skipped",
        "severity": "advisory",
        "ecosystem": "rust",
    }]


def test_no_backend_is_advisory_never_block(tmp_path, monkeypatch):
    _cargo_project(tmp_path)
    monkeypatch.setattr(egress_runner.shutil, "which", lambda name: "cargo")
    monkeypatch.setattr(egress_runner.sandbox, "available_backend", lambda pref: "")

    out = egress_runner.run_rust_hermetic(tmp_path, network="none")

    assert out["backend"] == ""
    assert out["findings"][0]["severity"] == "advisory"
    assert out["findings"][0]["reason"] == "no sandbox backend - rust egress containment not enforced here"


def test_probe_unsafe_rustc_wrapper_config_skips_without_cargo(tmp_path, monkeypatch):
    _cargo_project(tmp_path)
    config_dir = tmp_path / ".cargo"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text("[build]\nrustc-wrapper = \"evil\"\n", encoding="utf-8")
    monkeypatch.setattr(egress_runner.shutil, "which", lambda name: "cargo")
    monkeypatch.setattr(egress_runner.sandbox, "available_backend", lambda pref: "bwrap")
    monkeypatch.setattr(egress_runner, "_run_cargo", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))

    out = egress_runner.run_rust_hermetic(tmp_path, network="none")

    assert out["findings"][0]["severity"] == "advisory"
    assert out["findings"][0]["reason"] == "rust project config is not probe-safe - hermetic probe skipped"


def test_probe_unsafe_legacy_config_skips_without_cargo(tmp_path, monkeypatch):
    _cargo_project(tmp_path)
    config_dir = tmp_path / ".cargo"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text("[build]\n", encoding="utf-8")
    (config_dir / "config").write_text("[build]\nrustc-wrapper = \"evil\"\n", encoding="utf-8")
    monkeypatch.setattr(egress_runner.shutil, "which", lambda name: "cargo")
    monkeypatch.setattr(egress_runner.sandbox, "available_backend", lambda pref: "bwrap")
    monkeypatch.setattr(egress_runner, "_run_cargo", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))

    out = egress_runner.run_rust_hermetic(tmp_path, network="none")

    assert out["findings"][0]["severity"] == "advisory"
    assert out["findings"][0]["reason"] == "rust project config is not probe-safe - hermetic probe skipped"


def test_probe_unsafe_env_config_skips_without_cargo(tmp_path, monkeypatch):
    _cargo_project(tmp_path)
    config_dir = tmp_path / ".cargo"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text("[env]\nRUSTC_WRAPPER = \"evil\"\n", encoding="utf-8")
    monkeypatch.setattr(egress_runner.shutil, "which", lambda name: "cargo")
    monkeypatch.setattr(egress_runner.sandbox, "available_backend", lambda pref: "bwrap")
    monkeypatch.setattr(egress_runner, "_run_cargo", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))

    out = egress_runner.run_rust_hermetic(tmp_path, network="none")

    assert out["findings"][0]["reason"] == "rust project config is not probe-safe - hermetic probe skipped"


def test_probe_unsafe_credential_provider_config_skips_without_cargo(tmp_path, monkeypatch):
    _cargo_project(tmp_path)
    config_dir = tmp_path / ".cargo"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text("[registry]\ncredential-provider = \"evil\"\n", encoding="utf-8")
    monkeypatch.setattr(egress_runner.shutil, "which", lambda name: "cargo")
    monkeypatch.setattr(egress_runner.sandbox, "available_backend", lambda pref: "bwrap")
    monkeypatch.setattr(egress_runner, "_run_cargo", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))

    out = egress_runner.run_rust_hermetic(tmp_path, network="none")

    assert out["findings"][0]["reason"] == "rust project config is not probe-safe - hermetic probe skipped"


def test_probe_unsafe_rustflags_config_skips_without_cargo(tmp_path, monkeypatch):
    _cargo_project(tmp_path)
    config_dir = tmp_path / ".cargo"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text("[build]\nrustflags = [\"-Clinker=evil\"]\n", encoding="utf-8")
    monkeypatch.setattr(egress_runner.shutil, "which", lambda name: "cargo")
    monkeypatch.setattr(egress_runner.sandbox, "available_backend", lambda pref: "bwrap")
    monkeypatch.setattr(egress_runner, "_run_cargo", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))

    out = egress_runner.run_rust_hermetic(tmp_path, network="none")

    assert out["findings"][0]["reason"] == "rust project config is not probe-safe - hermetic probe skipped"


def test_probe_unsafe_include_config_skips_without_cargo(tmp_path, monkeypatch):
    _cargo_project(tmp_path)
    config_dir = tmp_path / ".cargo"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text("include = [\"evil.toml\"]\n", encoding="utf-8")
    monkeypatch.setattr(egress_runner.shutil, "which", lambda name: "cargo")
    monkeypatch.setattr(egress_runner.sandbox, "available_backend", lambda pref: "bwrap")
    monkeypatch.setattr(egress_runner, "_run_cargo", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))

    out = egress_runner.run_rust_hermetic(tmp_path, network="none")

    assert out["findings"][0]["severity"] == "advisory"
    assert out["findings"][0]["reason"] == "rust project config is not probe-safe - hermetic probe skipped"


def test_probe_unsafe_symlinked_cargo_dir_skips_without_cargo(tmp_path, monkeypatch):
    _cargo_project(tmp_path)
    real_config_dir = tmp_path / "real-cargo"
    real_config_dir.mkdir()
    (real_config_dir / "config.toml").write_text("[build]\nrustc-wrapper = \"evil\"\n", encoding="utf-8")
    try:
        os.symlink(real_config_dir, tmp_path / ".cargo", target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink unavailable: {exc}")
    monkeypatch.setattr(egress_runner.shutil, "which", lambda name: "cargo")
    monkeypatch.setattr(egress_runner.sandbox, "available_backend", lambda pref: "bwrap")
    monkeypatch.setattr(egress_runner, "_run_cargo", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))

    out = egress_runner.run_rust_hermetic(tmp_path, network="none")

    assert out["findings"][0]["severity"] == "advisory"
    assert out["findings"][0]["reason"] == "rust project config is not probe-safe - hermetic probe skipped"


def test_probe_unsafe_path_toolchain_skips_without_cargo(tmp_path, monkeypatch):
    _cargo_project(tmp_path)
    (tmp_path / "rust-toolchain.toml").write_text("[toolchain]\npath = \"./evil-toolchain\"\n", encoding="utf-8")
    monkeypatch.setattr(egress_runner.shutil, "which", lambda name: "cargo")
    monkeypatch.setattr(egress_runner.sandbox, "available_backend", lambda pref: "bwrap")
    monkeypatch.setattr(egress_runner, "_run_cargo", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))

    out = egress_runner.run_rust_hermetic(tmp_path, network="none")

    assert out["findings"][0]["reason"] == "rust project config is not probe-safe - hermetic probe skipped"


def test_network_open_does_nothing(tmp_path, monkeypatch):
    _cargo_project(tmp_path)
    monkeypatch.setattr(egress_runner, "_run_cargo", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))

    out = egress_runner.run_rust_hermetic(tmp_path, network="open")

    assert out["ran"] is False
    assert out["findings"] == []


def test_neutralized_env_drops_rustc_wrapper(tmp_path, monkeypatch):
    _cargo_project(tmp_path)
    monkeypatch.setenv("RUSTC_WRAPPER", "evil")
    monkeypatch.setenv("RUSTUP_TOOLCHAIN", "evil")
    monkeypatch.setenv("CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_RUNNER", "evil")
    calls = _setup(monkeypatch, [_CP(), _CP(), _CP()])

    egress_runner.run_rust_hermetic(tmp_path, network="none")

    env = calls[0][2]
    assert "RUSTC_WRAPPER" not in env
    assert "CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_RUNNER" not in env
    assert env["CARGO_HOME"]
    assert env["CARGO_TARGET_DIR"]
    assert env["PYTHONIOENCODING"] == "utf-8"
    assert env["RUSTUP_TOOLCHAIN"] == "stable"


def test_timeout_is_advisory(tmp_path, monkeypatch):
    _cargo_project(tmp_path)
    _setup(monkeypatch, [
        _CP(),
        subprocess.TimeoutExpired("cargo build", 1, output="partial", stderr="late"),
    ])

    out = egress_runner.run_rust_hermetic(tmp_path, network="none")

    assert out["findings"][0]["severity"] == "advisory"
    assert out["findings"][0]["reason"] == "rust hermetic probe timed out"
    assert out["findings"][0]["output_tail"] == "partial\nlate"
