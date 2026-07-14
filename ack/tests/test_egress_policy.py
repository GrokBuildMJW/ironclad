"""Approved-design egress policy reader for always-on advance enforcement."""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=lambda **kw: object()))
_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402


OPEN_POLICY = {"network": "open", "allow": [], "deny": []}
ABSENT_POLICY = {"network": "absent", "allow": [], "deny": []}


def _setup(monkeypatch, tmp_path):
    gx10._apply_config(gx10._code_defaults())
    monkeypatch.setattr(gx10, "FRAMING_NOTES_ENABLED", False)
    gx10.STORE = None
    monkeypatch.setattr(gx10, "_ui_print", lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)
    gx10.initiative_new("Demo", "software")
    return gx10.active_slug()


def _record_approved(body: str, **typed):
    gx10.record_design("Approach", body, **typed)
    assert gx10._approve_design().startswith("OK")


def _decision_doc():
    return gx10.vault_root() / gx10.active_slug() / "decisions" / "design.md"


def test_design_egress_policy_reads_network_none(monkeypatch, tmp_path):
    slug = _setup(monkeypatch, tmp_path)
    _record_approved("Use Python.\n\n## Build policy\n\nnetwork: none\n")

    assert gx10._design_egress_policy(slug) == {"network": "none", "allow": [], "deny": []}


def test_design_egress_policy_reads_network_declared(monkeypatch, tmp_path):
    slug = _setup(monkeypatch, tmp_path)
    _record_approved("Use Python.\n\n## Build policy\n\nNETWORK: declared\n")

    assert gx10._design_egress_policy(slug) == {"network": "declared", "allow": [], "deny": []}


def test_design_egress_policy_reads_network_open(monkeypatch, tmp_path):
    slug = _setup(monkeypatch, tmp_path)
    _record_approved("Use Python.\n\n## Build policy\n\nnetwork: open\n")

    assert gx10._design_egress_policy(slug) == OPEN_POLICY


def test_design_egress_policy_present_section_missing_network_is_invalid(monkeypatch, tmp_path):
    slug = _setup(monkeypatch, tmp_path)
    _record_approved("Use Python.\n\n## Build policy\n\nallow: requests\n")

    assert gx10._design_egress_policy(slug) == {
        "network": "invalid", "allow": ["requests"], "deny": []
    }


def test_design_egress_policy_unknown_network_is_invalid(monkeypatch, tmp_path):
    slug = _setup(monkeypatch, tmp_path)
    _record_approved("Use Python.\n\n## Build policy\n\nnetwork: maybe\n")

    assert gx10._design_egress_policy(slug) == {"network": "invalid", "allow": [], "deny": []}


def test_design_egress_policy_parses_allow_and_deny(monkeypatch, tmp_path):
    slug = _setup(monkeypatch, tmp_path)
    _record_approved(
        "Use Python.\n\n"
        "## Build policy\n\n"
        "network: declared\n"
        "allow: Requests,  HTTPX aiohttp\n"
        "deny:  Boto3, urllib3\n"
    )

    assert gx10._design_egress_policy(slug) == {
        "network": "declared",
        "allow": ["requests", "httpx", "aiohttp"],
        "deny": ["boto3", "urllib3"],
    }


def test_design_egress_policy_malformed_legacy_decision_refuses_migration(monkeypatch, tmp_path):
    slug = _setup(monkeypatch, tmp_path)
    doc = gx10.vault_root() / slug / "decisions" / "design.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_bytes(b"---\napproved: true\n---\n## Build policy\n\nnetwork: none\n\xff")

    with pytest.raises(gx10.DesignMigrationRefusal, match="not valid UTF-8"):
        gx10._design_egress_policy(slug)


def test_design_egress_policy_unapproved_is_absent(monkeypatch, tmp_path):
    slug = _setup(monkeypatch, tmp_path)
    _record_approved("Use Python.\n\n## Build policy\n\nnetwork: none\n")
    text = _decision_doc().read_text(encoding="utf-8").replace("approved: true", "approved: false")
    _decision_doc().write_text(text, encoding="utf-8")

    assert gx10._design_egress_policy(slug) == ABSENT_POLICY


def test_design_egress_policy_oversized_legacy_decision_refuses_migration(monkeypatch, tmp_path):
    slug = _setup(monkeypatch, tmp_path)
    doc = gx10.vault_root() / slug / "decisions" / "design.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text("---\napproved: true\n---\n" + ("x" * 65537), encoding="utf-8")

    with pytest.raises(gx10.DesignMigrationRefusal, match="exceeds the 65536-byte limit"):
        gx10._design_egress_policy(slug)


def test_design_egress_policy_absent_section_is_absent(monkeypatch, tmp_path):
    slug = _setup(monkeypatch, tmp_path)
    _record_approved("Use Python.")

    assert gx10._design_egress_policy(slug) == ABSENT_POLICY


def test_design_egress_policy_empty_slug_is_absent(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)

    assert gx10._design_egress_policy("") == ABSENT_POLICY


def test_design_egress_policy_non_migration_read_error_is_invalid(monkeypatch, tmp_path):
    slug = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(gx10, "_approved_design_build_policy_section",
                        lambda _slug: (_ for _ in ()).throw(OSError("unreadable")))

    assert gx10._design_egress_policy(slug) == {"network": "invalid", "allow": [], "deny": []}


def test_egress_policy_network_does_not_reactivate_build_hardcheck(monkeypatch, tmp_path):
    slug = _setup(monkeypatch, tmp_path)
    _record_approved("Use Python.\n\n## Build policy\n\nnetwork: none\n", language="python")

    assert gx10._design_typed(slug) == {"language": "python"}
    assert gx10._design_build_check(slug, {"language": "python"}) is None
    assert gx10._design_build_check(slug, {"language": "python", "network": True}) is None
    assert gx10._design_build_check(slug, {"language": "rust"}) is not None
