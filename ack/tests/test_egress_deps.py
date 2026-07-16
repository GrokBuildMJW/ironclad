from __future__ import annotations

from ack.egress import KNOWN_EGRESS_VERSION, analyze_dependencies, canonicalize_name, resolve_closure


def _write_requirements(tmp_path, requirement="requests==2.32.4"):
    (tmp_path / "requirements.txt").write_text(f"{requirement}\n", encoding="utf-8")


def test_resolve_closure_pinned_requirements_is_full_closure(tmp_path):
    (tmp_path / "requirements.txt").write_text(
        "Requests==2.32.4\nurllib3[secure]==2.5.0 ; python_version >= '3.11'\n",
        encoding="utf-8",
    )

    assert resolve_closure(tmp_path) == ({"requests", "urllib3"}, True)


def test_resolve_closure_unpinned_requirements_is_not_full_closure(tmp_path):
    (tmp_path / "requirements.txt").write_text("requests>=2\npytest==8.4.1\n", encoding="utf-8")

    assert resolve_closure(tmp_path) == ({"requests", "pytest"}, False)


def test_resolve_closure_requirement_include_is_not_full_closure(tmp_path):
    (tmp_path / "requirements.txt").write_text("requests==2.32.4\n-r other.txt\n", encoding="utf-8")

    assert resolve_closure(tmp_path) == ({"requests"}, False)


def test_resolve_closure_editable_requirement_is_not_full_closure(tmp_path):
    (tmp_path / "requirements.txt").write_text("-e .\n", encoding="utf-8")

    assert resolve_closure(tmp_path) == (set(), False)


def test_resolve_closure_url_requirement_is_not_full_closure(tmp_path):
    (tmp_path / "requirements.txt").write_text(
        "requests==2.32.4\ngit+https://example.invalid/repo.git\n",
        encoding="utf-8",
    )

    assert resolve_closure(tmp_path) == ({"requests"}, False)


def test_resolve_closure_pyproject_direct_only(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "[project]\n"
        "dependencies = ['HTTPX>=0.28', 'rich']\n"
        "[project.optional-dependencies]\n"
        "dev = ['PyTest==8.4.1']\n",
        encoding="utf-8",
    )

    assert resolve_closure(tmp_path) == ({"httpx", "rich", "pytest"}, False)


def test_resolve_closure_absent_returns_empty_not_full(tmp_path):
    assert resolve_closure(tmp_path) == (set(), False)


def test_resolve_closure_malformed_fails_soft(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project\n", encoding="utf-8")

    assert resolve_closure(tmp_path) == (set(), False)


def test_analyze_network_none_known_egress_blocks(tmp_path):
    (tmp_path / "requirements.txt").write_text("requests==2.32.4\n", encoding="utf-8")

    result = analyze_dependencies(tmp_path, {"network": "none", "allow": [], "deny": []})

    assert result["findings"] == [
        {
            "package": "requests",
            "reason": "known egress-capable dependency is not allow-listed for network:none",
            "severity": "block",
        }
    ]


def test_analyze_network_none_allow_list_suppresses_known_egress(tmp_path):
    (tmp_path / "requirements.txt").write_text("Requests==2.32.4\n", encoding="utf-8")

    result = analyze_dependencies(tmp_path, {"network": "none", "allow": ["requests"], "deny": []})

    assert result["findings"] == []


def test_analyze_network_declared_known_egress_is_advisory(tmp_path):
    (tmp_path / "requirements.txt").write_text("httpx==0.28.1\n", encoding="utf-8")

    result = analyze_dependencies(tmp_path, {"network": "declared", "allow": [], "deny": []})

    assert result["findings"] == [
        {
            "package": "httpx",
            "reason": "known egress-capable dependency under network:declared",
            "severity": "advisory",
        }
    ]


def test_analyze_explicit_deny_dep_in_closure_finds_block(tmp_path):
    (tmp_path / "requirements.txt").write_text("rich==13.9.4\n", encoding="utf-8")

    result = analyze_dependencies(tmp_path, {"network": "open", "allow": [], "deny": ["rich"]})

    assert result["findings"] == [
        {"package": "rich", "reason": "explicitly denied by egress policy", "severity": "block"}
    ]


def test_analyze_explicit_python_deny_blocks(tmp_path):
    _write_requirements(tmp_path)

    result = analyze_dependencies(tmp_path, {"network": "open", "allow": [], "deny": ["py:requests"]})

    assert result["findings"] == [
        {"package": "requests", "reason": "explicitly denied by egress policy", "severity": "block"}
    ]


def test_analyze_network_none_python_allow_suppresses_known_egress(tmp_path):
    _write_requirements(tmp_path)

    result = analyze_dependencies(tmp_path, {"network": "none", "allow": ["py:requests"], "deny": []})

    assert result["findings"] == []


def test_analyze_foreign_ecosystem_deny_does_not_block_python(tmp_path):
    _write_requirements(tmp_path)

    result = analyze_dependencies(tmp_path, {"network": "open", "allow": [], "deny": ["rust:requests"]})

    assert result["findings"] == []


def test_analyze_foreign_ecosystem_allow_does_not_suppress_python(tmp_path):
    # A foreign-ecosystem allow entry must be dropped, so it cannot suppress the
    # Python known-egress block under network:none (the allow side of the prefix bug).
    _write_requirements(tmp_path)

    result = analyze_dependencies(tmp_path, {"network": "none", "allow": ["rust:requests"], "deny": []})

    assert result["findings"] == [
        {
            "package": "requests",
            "reason": "known egress-capable dependency is not allow-listed for network:none",
            "severity": "block",
        }
    ]


def test_analyze_bare_policy_names_still_apply_to_python(tmp_path):
    _write_requirements(tmp_path)

    denied = analyze_dependencies(tmp_path, {"network": "open", "allow": [], "deny": ["requests"]})
    allowed = analyze_dependencies(tmp_path, {"network": "none", "allow": ["requests"], "deny": []})

    assert denied["findings"] == [
        {"package": "requests", "reason": "explicitly denied by egress policy", "severity": "block"}
    ]
    assert allowed["findings"] == []


def test_analyze_python_policy_name_is_canonicalized_after_prefix(tmp_path):
    _write_requirements(tmp_path, "Re.Quests==2.32.4")

    result = analyze_dependencies(tmp_path, {"network": "open", "allow": [], "deny": ["py:Re_quests"]})

    assert result["findings"] == [
        {"package": "re-quests", "reason": "explicitly denied by egress policy", "severity": "block"}
    ]


def test_analyze_network_open_known_egress_has_no_finding_without_deny(tmp_path):
    (tmp_path / "requirements.txt").write_text("urllib3==2.5.0\n", encoding="utf-8")

    result = analyze_dependencies(tmp_path, {"network": "open", "allow": [], "deny": []})

    assert result["findings"] == []


def test_analyze_surfaces_direct_only_when_full_closure_unavailable(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\ndependencies = ['requests']\n", encoding="utf-8")

    result = analyze_dependencies(tmp_path, {"network": "none", "allow": [], "deny": []})

    assert result["is_full_closure"] is False
    assert result["closure_size"] == 1
    assert result["findings"][0]["severity"] == "block"


def test_analyze_empty_requirements_falls_through_to_pyproject(tmp_path):
    (tmp_path / "requirements.txt").write_text("# only comments\n\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\ndependencies = ['requests']\n", encoding="utf-8")

    result = analyze_dependencies(tmp_path, {"network": "none", "allow": [], "deny": []})

    assert result["is_full_closure"] is False
    assert result["closure_size"] == 1
    assert result["findings"] == [
        {
            "package": "requests",
            "reason": "known egress-capable dependency is not allow-listed for network:none",
            "severity": "block",
        }
    ]


def test_name_canonicalization_normalizes_policy_and_requirements(tmp_path):
    (tmp_path / "requirements.txt").write_text("Requests==2.32.4\n", encoding="utf-8")

    result = analyze_dependencies(tmp_path, {"network": "none", "allow": ["Re_quests"], "deny": []})

    assert canonicalize_name("Re.Quests") == "re-quests"
    assert result["findings"][0]["package"] == "requests"


def test_analyze_is_fail_soft_on_garbage_inputs(tmp_path):
    result = analyze_dependencies(tmp_path / "missing", None)

    assert result == {"findings": [], "is_full_closure": False, "network": "open", "closure_size": 0}


def test_known_egress_set_is_versioned():
    assert KNOWN_EGRESS_VERSION == "2026.07.1"
