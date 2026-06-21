"""Skill library catalogue (#35): manifest index over both kinds + semver + provenance +
discover/install/update. Uses ack.skillgen to author the fixtures (dogfooding).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ack import catalogue as cat
from ack import skillgen


def _library(root: Path, *, tool_v="0.1.0", pb_v="0.1.0") -> Path:
    skillgen.write_scaffold(skillgen.SkillSpec(
        capability="csv-summarize", description="Summarize a CSV", kind="tool",
        domain="data", params=[("path", "str")], version=tool_v), root, force=True)
    skillgen.write_scaffold(skillgen.SkillSpec(
        capability="report-writing", description="Write a report", kind="playbook",
        type="capability", domain="writing", trigger=["write a report"], version=pb_v), root,
        force=True)
    return root


# ── version helpers ───────────────────────────────────────────
def test_version_compare():
    assert cat.is_newer("0.2.0", "0.1.9")
    assert cat.is_newer("1.0.0", "0.9.9")
    assert not cat.is_newer("0.1.0", "0.1.0")
    assert cat.parse_version("bad") == (0,)


# ── build catalogue over both kinds ───────────────────────────
def test_build_catalogue_indexes_both_kinds(tmp_path):
    lib = _library(tmp_path / "builtin")
    c = cat.build_catalogue([(str(lib), "built-in")])
    tool = c.get("csv-summarize")
    pbk = c.get("report-writing")
    assert tool.kind == "tool" and tool.domain == "data" and tool.version == "0.1.0"
    assert tool.provenance == "user"  # CASE carries provenance=user from the scaffold default
    assert pbk.kind == "playbook" and pbk.domain == "writing"
    assert {e["capability"] for e in c.index()} == {"csv-summarize", "report-writing"}
    assert len(c.by_kind("tool")) == 1 and len(c.by_kind("playbook")) == 1
    assert len(c.by_domain("writing")) == 1


def test_higher_version_wins_across_libraries(tmp_path):
    old = _library(tmp_path / "old", tool_v="0.1.0")
    new = _library(tmp_path / "new", tool_v="0.3.0")
    c = cat.build_catalogue([(str(old), "built-in"), (str(new), "user")])
    assert c.get("csv-summarize").version == "0.3.0"


# ── install + update ──────────────────────────────────────────
def test_install_tool_and_playbook(tmp_path):
    lib = _library(tmp_path / "lib")
    c = cat.build_catalogue([(str(lib), "user")])
    active = tmp_path / "active"
    tpath = cat.install(c.get("csv-summarize"), active)
    ppath = cat.install(c.get("report-writing"), active)
    assert tpath.is_file() and tpath.suffix == ".py"
    assert (ppath / "SKILL.md").is_file()
    # the installed skills are themselves discoverable
    found = {e for e in cat.build_catalogue([(str(active), "user")]).entries}
    assert {"csv-summarize", "report-writing"} <= found


def test_install_refuses_overwrite_then_allows(tmp_path):
    lib = _library(tmp_path / "lib")
    c = cat.build_catalogue([(str(lib), "user")])
    active = tmp_path / "active"
    cat.install(c.get("csv-summarize"), active)
    with pytest.raises(FileExistsError):
        cat.install(c.get("csv-summarize"), active)
    assert cat.install(c.get("csv-summarize"), active, overwrite=True)


def test_update_only_when_newer(tmp_path):
    active = tmp_path / "active"
    v1 = cat.build_catalogue([(str(_library(tmp_path / "v1", pb_v="0.1.0")), "user")])
    cat.install(v1.get("report-writing"), active)
    # same version → no update
    assert cat.update(v1.get("report-writing"), active) is None
    # newer version → updated
    v2 = cat.build_catalogue([(str(_library(tmp_path / "v2", pb_v="0.2.0")), "user")])
    updated = cat.update(v2.get("report-writing"), active)
    assert updated is not None
    assert cat.build_catalogue([(str(active), "user")]).get("report-writing").version == "0.2.0"
