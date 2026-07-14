from __future__ import annotations

from ack.egress.rust_deps import analyze_rust_dependencies, canonicalize_crate, resolve_rust_closure
from ack.egress.rust_known_egress import KNOWN_EGRESS_CRATES_VERSION


def test_resolve_rust_closure_cargo_lock_is_full_closure(tmp_path):
    (tmp_path / "Cargo.lock").write_text(
        "[[package]]\n"
        'name = "demo"\n'
        'version = "0.1.0"\n\n'
        "[[package]]\n"
        'name = "hyper_tls"\n'
        'version = "0.6.0"\n',
        encoding="utf-8",
    )

    assert resolve_rust_closure(tmp_path) == ({"demo", "hyper-tls"}, True)


def test_resolve_rust_closure_cargo_toml_direct_only(tmp_path):
    (tmp_path / "Cargo.toml").write_text(
        "[package]\n"
        'name = "demo"\n'
        'version = "0.1.0"\n'
        "[dependencies]\n"
        'Reqwest = "0.12"\n'
        'http-client = { package = "hyper_tls", version = "0.6" }\n'
        "[dev-dependencies]\n"
        'tokio = "1"\n'
        "[build-dependencies]\n"
        'cc = "1"\n',
        encoding="utf-8",
    )

    assert resolve_rust_closure(tmp_path) == ({"reqwest", "hyper-tls", "tokio", "cc"}, False)


def test_resolve_rust_closure_absent_returns_empty_not_full(tmp_path):
    assert resolve_rust_closure(tmp_path) == (set(), False)


def test_resolve_rust_closure_malformed_fails_soft(tmp_path):
    (tmp_path / "Cargo.lock").write_text("[[package]\n", encoding="utf-8")

    assert resolve_rust_closure(tmp_path) == (set(), False)


def test_analyze_network_none_known_egress_blocks(tmp_path):
    _write_lock(tmp_path, "reqwest")

    result = analyze_rust_dependencies(tmp_path, {"network": "none", "allow": [], "deny": []})

    assert result["findings"] == [
        {
            "package": "reqwest",
            "reason": "known egress-capable crate is not allow-listed for network:none",
            "severity": "block",
            "ecosystem": "rust",
        }
    ]


def test_analyze_network_none_rust_allow_suppresses_known_egress(tmp_path):
    _write_lock(tmp_path, "reqwest")

    result = analyze_rust_dependencies(tmp_path, {"network": "none", "allow": ["rust:reqwest"], "deny": []})

    assert result["findings"] == []


def test_analyze_network_none_bare_allow_suppresses_known_egress(tmp_path):
    _write_lock(tmp_path, "reqwest")

    result = analyze_rust_dependencies(tmp_path, {"network": "none", "allow": ["reqwest"], "deny": []})

    assert result["findings"] == []


def test_analyze_py_allow_does_not_suppress_rust_known_egress(tmp_path):
    _write_lock(tmp_path, "reqwest")

    result = analyze_rust_dependencies(tmp_path, {"network": "none", "allow": ["py:reqwest"], "deny": []})

    assert result["findings"][0]["severity"] == "block"


def test_analyze_explicit_rust_deny_blocks(tmp_path):
    _write_lock(tmp_path, "serde")

    result = analyze_rust_dependencies(tmp_path, {"network": "open", "allow": [], "deny": ["rust:serde"]})

    assert result["findings"] == [
        {
            "package": "serde",
            "reason": "explicitly denied by egress policy",
            "severity": "block",
            "ecosystem": "rust",
        }
    ]


def test_analyze_network_declared_known_egress_is_advisory(tmp_path):
    _write_lock(tmp_path, "reqwest")

    result = analyze_rust_dependencies(tmp_path, {"network": "declared", "allow": [], "deny": []})

    assert result["findings"] == [
        {
            "package": "reqwest",
            "reason": "known egress-capable crate under network:declared",
            "severity": "advisory",
            "ecosystem": "rust",
        }
    ]


def test_analyze_network_open_known_egress_is_silent(tmp_path):
    _write_lock(tmp_path, "reqwest")

    result = analyze_rust_dependencies(tmp_path, {"network": "open", "allow": [], "deny": []})

    assert result["findings"] == []


def test_feature_gated_unresolved_is_advisory_not_block(tmp_path):
    _write_lock(tmp_path, "tokio")

    result = analyze_rust_dependencies(tmp_path, {"network": "none", "allow": [], "deny": []})

    assert result["findings"] == [
        {
            "package": "tokio",
            "reason": "feature-gated egress crate; features not resolved",
            "severity": "advisory",
            "ecosystem": "rust",
        }
    ]


def test_feature_gated_enabled_egress_feature_blocks_network_none(tmp_path):
    _write_lock(tmp_path, "tokio")

    result = analyze_rust_dependencies(
        tmp_path,
        {"network": "none", "allow": [], "deny": []},
        active_features={"tokio": {"net"}},
    )

    assert result["findings"] == [
        {
            "package": "tokio",
            "reason": "egress-capable feature is enabled and is not allow-listed for network:none",
            "severity": "block",
            "ecosystem": "rust",
        }
    ]


def test_feature_gated_non_egress_feature_is_advisory(tmp_path):
    _write_lock(tmp_path, "tokio")

    result = analyze_rust_dependencies(
        tmp_path,
        {"network": "none", "allow": [], "deny": []},
        active_features={"tokio": {"rt"}},
    )

    assert result["findings"] == [
        {
            "package": "tokio",
            "reason": "feature-gated egress crate; egress feature not enabled",
            "severity": "advisory",
            "ecosystem": "rust",
        }
    ]


def test_cargo_canonicalization_matches_hyphen_and_underscore(tmp_path):
    _write_lock(tmp_path, "hyper_tls")

    result = analyze_rust_dependencies(tmp_path, {"network": "none", "allow": ["rust:hyper-tls"], "deny": []})

    assert canonicalize_crate("Hyper_TLS") == "hyper-tls"
    assert result["findings"] == []


def test_analyze_is_fail_soft_on_garbage_inputs(tmp_path):
    result = analyze_rust_dependencies(tmp_path / "missing", None, active_features=object())

    assert result == {"findings": [], "is_full_closure": False, "network": "open", "closure_size": 0}


def test_known_egress_crate_set_is_versioned():
    assert KNOWN_EGRESS_CRATES_VERSION == "2026.07.1"


def _write_lock(root, *names):
    root.joinpath("Cargo.lock").write_text(
        "".join(f'[[package]]\nname = "{name}"\nversion = "0.1.0"\n\n' for name in names),
        encoding="utf-8",
    )
