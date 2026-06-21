"""#90 — mpr migrated as the reference built-in: its CASE carries the catalogue manifest
fields (type/version/provenance) and ack.catalogue indexes it, with no contract change
(back-compat: the existing CASE keys remain; behavior is unchanged).
"""
from __future__ import annotations

from pathlib import Path

from mpr.entry import build_case


def test_case_has_manifest_fields_and_is_backcompat():
    c = build_case()
    # back-compat: the existing contract keys are unchanged
    assert c["capability"] == "mpr_research" and c["name"] == "mpr_research"
    assert c["domain"] == "reasoning" and c["description"]
    # migration: the catalogue manifest fields are present
    assert c["version"] == "0.1.0"
    assert c["type"] == "capability"
    assert c["provenance"] == "built-in"


def test_catalogue_indexes_mpr_as_builtin(monkeypatch):
    monkeypatch.setenv("GX10_MPR", "1")   # the A/B gate must be on for the CASE to be exposed
    from ack import catalogue as cat

    mpr_root = Path(__file__).resolve().parents[1]   # skills/mpr
    c = cat.build_catalogue([(str(mpr_root), "built-in")])
    entry = c.get("mpr_research")
    assert entry is not None, "mpr_research not indexed by the catalogue"
    assert entry.kind == "tool"
    assert entry.version == "0.1.0"
    assert entry.provenance == "built-in"
    assert entry.domain == "reasoning"
