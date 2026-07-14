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


def _files(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def _swap_artifacts(active: Path) -> list[str]:
    skills = active / "skills"
    return sorted(
        path.name for path in skills.iterdir()
        if path.name.endswith((".staging", ".backup"))
    )


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
    assert tpath == active / "skills" / Path(c.get("csv-summarize").source).name
    assert ppath == active / "skills" / Path(c.get("report-writing").source).name
    assert tpath.is_file() and tpath.suffix == ".py"
    assert (ppath / "SKILL.md").is_file()
    assert _swap_artifacts(active) == []
    # the installed skills are themselves discoverable
    found = {e for e in cat.build_catalogue([(str(active), "user")]).entries}
    assert {"csv-summarize", "report-writing"} <= found


def test_install_refuses_overwrite_for_both_kinds(tmp_path):
    lib = _library(tmp_path / "lib")
    c = cat.build_catalogue([(str(lib), "user")])
    active = tmp_path / "active"
    entries = [c.get("csv-summarize"), c.get("report-writing")]
    for entry in entries:
        cat.install(entry, active)
    for entry in entries:
        with pytest.raises(FileExistsError):
            cat.install(entry, active, overwrite=False)
    assert _swap_artifacts(active) == []


def test_playbook_overwrite_copy_failure_preserves_original(tmp_path, monkeypatch):
    active = tmp_path / "active"
    old = cat.build_catalogue([
        (str(_library(tmp_path / "old", pb_v="0.1.0")), "user"),
    ]).get("report-writing")
    new = cat.build_catalogue([
        (str(_library(tmp_path / "new", pb_v="0.2.0")), "user"),
    ]).get("report-writing")
    target = cat.install(old, active)
    original = _files(target)

    def fail_copytree(_src, staging, **_kwargs):
        (Path(staging) / "partial.txt").write_text("partial", encoding="utf-8")
        raise OSError("copy failed")

    monkeypatch.setattr(cat.shutil, "copytree", fail_copytree)
    with pytest.raises(OSError, match="copy failed"):
        cat.install(new, active, overwrite=True)

    assert _files(target) == original
    assert _swap_artifacts(active) == []


def test_playbook_overwrite_swap_failure_restores_original(tmp_path, monkeypatch):
    active = tmp_path / "active"
    old = cat.build_catalogue([
        (str(_library(tmp_path / "old", pb_v="0.1.0")), "user"),
    ]).get("report-writing")
    new = cat.build_catalogue([
        (str(_library(tmp_path / "new", pb_v="0.2.0")), "user"),
    ]).get("report-writing")
    target = cat.install(old, active)
    original = _files(target)
    real_replace = cat.os.replace

    def fail_staging_swap(src, dst):
        if Path(src).name.endswith(".staging"):
            raise OSError("swap failed")
        return real_replace(src, dst)

    monkeypatch.setattr(cat.os, "replace", fail_staging_swap)
    with pytest.raises(OSError, match="swap failed"):
        cat.install(new, active, overwrite=True)

    assert _files(target) == original
    assert _swap_artifacts(active) == []


def test_playbook_overwrite_survives_backup_cleanup_failure(tmp_path, monkeypatch):
    # #1493 review (#1 + #3): a completed swap must NOT report failure just because the now-obsolete backup
    # can't be removed (Windows lock/AV), and a leftover `.backup` must never shadow the freshly-installed
    # version in discovery.
    active = tmp_path / "active"
    cat.install(cat.build_catalogue([(str(_library(tmp_path / "old", pb_v="0.1.0")), "user")]).get("report-writing"), active)
    new = cat.build_catalogue([(str(_library(tmp_path / "new", pb_v="0.2.0")), "user")]).get("report-writing")

    real_rmtree = cat.shutil.rmtree

    def stubborn_rmtree(path, ignore_errors=False, **kw):
        if str(path).endswith(".backup"):
            if ignore_errors:
                return  # simulate a lock: cannot remove, but ignore_errors swallows it (no raise into install)
            raise OSError("backup locked")
        return real_rmtree(path, ignore_errors=ignore_errors, **kw)

    monkeypatch.setattr(cat.shutil, "rmtree", stubborn_rmtree)
    target = cat.install(new, active, overwrite=True)  # MUST NOT raise despite the backup lock
    assert (target / "SKILL.md").is_file()
    # the leftover `.backup` (a stale 0.1.0 copy) must not shadow the new 0.2.0 in discovery
    assert cat.build_catalogue([(str(active), "user")]).get("report-writing").version == "0.2.0"


def test_playbook_overwrite_double_fault_preserves_original_in_backup(tmp_path, monkeypatch):
    # #1493 review (#2, data-safety): if the swap fails AND the restore of the old install also fails, the ONLY
    # surviving copy of the original is in `.backup` — the finally must NOT delete it.
    active = tmp_path / "active"
    old = cat.build_catalogue([(str(_library(tmp_path / "old", pb_v="0.1.0")), "user")]).get("report-writing")
    new = cat.build_catalogue([(str(_library(tmp_path / "new", pb_v="0.2.0")), "user")]).get("report-writing")
    target = cat.install(old, active)
    original = _files(target)
    real_replace = cat.os.replace

    def fail_every_move_into_target(src, dst):
        if Path(dst) == target:            # fail BOTH staging→target AND the backup→target restore
            raise OSError("target locked")
        return real_replace(src, dst)      # allow target→backup (moving the old aside)

    monkeypatch.setattr(cat.os, "replace", fail_every_move_into_target)
    with pytest.raises(OSError, match="target locked"):
        cat.install(new, active, overwrite=True)

    assert not target.exists()             # the failed swap never created the target
    backups = [p for p in (active / "skills").iterdir() if p.name.endswith(".backup")]
    assert len(backups) == 1 and _files(backups[0]) == original  # the original is preserved, not destroyed


def test_discover_skips_dotdir_playbook_leftovers(tmp_path):
    # #1493 review (#3): discovery must ignore `.`-prefixed dirs so an interrupted install's `.staging`/
    # `.backup` copy is never surfaced as a (shadowing) skill.
    from ack.playbook import discover_playbooks
    lib = _library(tmp_path / "lib", pb_v="0.2.0")
    # a stale leftover copy of the SAME capability under a hidden staging/backup dir
    real = lib / "skills" / "report-writing"
    leftover = lib / "skills" / ".report-writing.abcd.backup"
    shutil_copytree = __import__("shutil").copytree
    shutil_copytree(real, leftover)
    caps = [pb.capability for pb in discover_playbooks(lib)]
    assert caps.count("report-writing") == 1  # the leftover was skipped, not a duplicate
    assert cat.build_catalogue([(str(lib), "user")]).get("report-writing").version == "0.2.0"


def test_tool_overwrite_copy_failure_preserves_original(tmp_path, monkeypatch):
    active = tmp_path / "active"
    old = cat.build_catalogue([
        (str(_library(tmp_path / "old", tool_v="0.1.0")), "user"),
    ]).get("csv-summarize")
    new = cat.build_catalogue([
        (str(_library(tmp_path / "new", tool_v="0.2.0")), "user"),
    ]).get("csv-summarize")
    target = cat.install(old, active)
    original = target.read_bytes()

    def fail_copy2(_src, staging, **_kwargs):
        Path(staging).write_bytes(b"partial")
        raise OSError("copy failed")

    monkeypatch.setattr(cat.shutil, "copy2", fail_copy2)
    with pytest.raises(OSError, match="copy failed"):
        cat.install(new, active, overwrite=True)

    assert target.read_bytes() == original
    assert _swap_artifacts(active) == []


def test_overwrite_replaces_both_kinds_atomically(tmp_path):
    active = tmp_path / "active"
    old = cat.build_catalogue([
        (str(_library(tmp_path / "old", tool_v="0.1.0", pb_v="0.1.0")), "user"),
    ])
    new = cat.build_catalogue([
        (str(_library(tmp_path / "new", tool_v="0.2.0", pb_v="0.2.0")), "user"),
    ])
    for capability in ("csv-summarize", "report-writing"):
        cat.install(old.get(capability), active)

    tool = new.get("csv-summarize")
    playbook = new.get("report-writing")
    tool_target = cat.install(tool, active, overwrite=True)
    playbook_target = cat.install(playbook, active, overwrite=True)

    assert tool_target == active / "skills" / Path(tool.source).name
    assert playbook_target == active / "skills" / Path(playbook.source).name
    assert tool_target.read_bytes() == Path(tool.source).read_bytes()
    assert _files(playbook_target) == _files(Path(playbook.source))
    assert _swap_artifacts(active) == []


def test_update_only_when_newer(tmp_path):
    active = tmp_path / "active"
    current = cat.build_catalogue([(str(_library(tmp_path / "current", pb_v="0.2.0")), "user")])
    cat.install(current.get("report-writing"), active)
    # older version → no update
    older = cat.build_catalogue([(str(_library(tmp_path / "older", pb_v="0.1.0")), "user")])
    assert cat.update(older.get("report-writing"), active) is None
    # same version → no update
    assert cat.update(current.get("report-writing"), active) is None
    # newer version → updated
    newer = cat.build_catalogue([(str(_library(tmp_path / "newer", pb_v="0.3.0")), "user")])
    updated = cat.update(newer.get("report-writing"), active)
    assert updated is not None
    assert cat.build_catalogue([(str(active), "user")]).get("report-writing").version == "0.3.0"
