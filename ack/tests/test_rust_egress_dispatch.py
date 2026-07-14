from __future__ import annotations

import sys

from ack.egress import deps, rust_deps
from ack.egress.deps import analyze_dependencies
from engine import egress_runner


def test_polyglot_dependency_dispatch_merges_python_and_rust(tmp_path):
    (tmp_path / "requirements.txt").write_text("requests==2.32.4\n", encoding="utf-8")
    _write_cargo_project(tmp_path)
    _write_lock(tmp_path, "reqwest")

    result = analyze_dependencies(tmp_path, {"network": "none", "allow": [], "deny": []})

    assert set(result) == {"findings", "is_full_closure", "network", "closure_size"}
    assert result["is_full_closure"] is True
    assert result["closure_size"] == 2
    assert any(finding["package"] == "requests" and "ecosystem" not in finding for finding in result["findings"])
    assert any(
        finding["package"] == "reqwest" and finding.get("ecosystem") == "rust"
        for finding in result["findings"]
    )


def test_rust_only_network_none_reqwest_blocks(tmp_path):
    _write_cargo_project(tmp_path)
    _write_lock(tmp_path, "reqwest")

    result = analyze_dependencies(tmp_path, {"network": "none", "allow": [], "deny": []})

    assert result["is_full_closure"] is True
    assert result["closure_size"] == 1
    assert result["findings"] == [{
        "package": "reqwest",
        "reason": "known egress-capable crate is not allow-listed for network:none",
        "severity": "block",
        "ecosystem": "rust",
    }]


def test_rust_exception_preserves_python_findings(tmp_path, monkeypatch):
    (tmp_path / "requirements.txt").write_text("requests==2.32.4\n", encoding="utf-8")
    _write_cargo_project(tmp_path)

    def _boom(*args, **kwargs):
        raise RuntimeError("broken analyzer")

    monkeypatch.setattr(rust_deps, "analyze_rust_dependencies", _boom)

    result = analyze_dependencies(tmp_path, {"network": "none", "allow": [], "deny": []})

    assert any(finding.get("package") == "requests" and finding["severity"] == "block"
               for finding in result["findings"])
    assert any(
        finding.get("ecosystem") == "rust"
        and finding["severity"] == "advisory"
        and finding["reason"] == "rust egress analysis skipped (internal)"
        for finding in result["findings"]
    )


def test_rust_feature_resolver_injection_controls_feature_gated_severity(tmp_path):
    _write_cargo_project(tmp_path)
    _write_lock(tmp_path, "tokio")

    unresolved = analyze_dependencies(tmp_path, {"network": "none", "allow": [], "deny": []})
    resolved = analyze_dependencies(
        tmp_path,
        {"network": "none", "allow": [], "deny": []},
        rust_feature_resolver=lambda root: {"tokio": {"net"}},
    )

    assert unresolved["findings"][0]["severity"] == "advisory"
    assert unresolved["findings"][0]["reason"] == "feature-gated egress crate; features not resolved"
    assert resolved["findings"][0]["severity"] == "block"
    assert resolved["findings"][0]["reason"] == "egress-capable feature is enabled and is not allow-listed for network:none"


def test_run_hermetic_python_only_is_byte_identical(tmp_path, monkeypatch):
    (tmp_path / "tests").mkdir()
    monkeypatch.setattr(egress_runner.sandbox, "available_backend", lambda pref: "bwrap")
    monkeypatch.setattr(egress_runner, "_run_command", lambda *a, **k: _CP())

    expected = egress_runner._run_python_hermetic(tmp_path, network="none")
    actual = egress_runner.run_hermetic(tmp_path, network="none")

    assert actual == expected


def test_run_hermetic_cargo_tree_also_runs_rust_probe(tmp_path, monkeypatch):
    (tmp_path / "tests").mkdir()
    _write_cargo_project(tmp_path)
    monkeypatch.setattr(egress_runner.sandbox, "available_backend", lambda pref: "bwrap")
    monkeypatch.setattr(egress_runner, "_run_command", lambda *a, **k: _CP())

    rust_calls = []

    def _rust_probe(root, *, network, sandbox_pref="auto"):
        rust_calls.append((root, network, sandbox_pref))
        return {
            "ran": True,
            "contained": True,
            "backend": "bwrap",
            "commands": ["cargo build --frozen --offline"],
            "findings": [{"command": "cargo build --frozen --offline", "reason": "ok", "severity": "advisory",
                          "ecosystem": "rust"}],
        }

    monkeypatch.setattr(egress_runner, "run_rust_hermetic", _rust_probe)

    result = egress_runner.run_hermetic(tmp_path, network="none")

    assert rust_calls == [(tmp_path, "none", "auto")]
    assert result["ran"] is True
    assert result["contained"] is True
    assert any(command == "cargo build --frozen --offline" for command in result["commands"])
    assert any(finding.get("ecosystem") == "rust" for finding in result["findings"])


def test_gx10_passes_rust_feature_resolver_to_dependency_analysis(tmp_path, monkeypatch):
    from engine import gx10

    captured = {}

    def _analyze(root, pol, *, rust_feature_resolver=None):
        captured["resolver"] = rust_feature_resolver
        return {"findings": []}

    monkeypatch.setattr(deps, "analyze_dependencies", _analyze)
    monkeypatch.setattr(sys.modules["ack.egress"], "analyze_dependencies", _analyze)
    monkeypatch.setattr("ack.egress.staticscan.scan_source_tree", lambda root: {"findings": []})
    monkeypatch.setattr(egress_runner, "run_hermetic", lambda root, *, network: {"findings": []})

    gx10._egress_advance_findings(tmp_path, {"network": "none"})

    assert captured["resolver"] is egress_runner.rust_feature_resolver


class _CP:
    returncode = 0
    stdout = ""
    stderr = ""


def _write_cargo_project(root):
    (root / "Cargo.toml").write_text("[package]\nname = \"demo\"\nversion = \"0.1.0\"\n", encoding="utf-8")


def _write_lock(root, *names):
    root.joinpath("Cargo.lock").write_text(
        "".join(f'[[package]]\nname = "{name}"\nversion = "0.1.0"\n\n' for name in names),
        encoding="utf-8",
    )
