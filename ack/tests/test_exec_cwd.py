"""S9c exec-cwd seam: ProjectContext drives _exec_cwd, _resolve_exec_path, and run_tool.

These tests live in ack/tests but exercise core/engine modules via sys.path.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import project_context as pc  # noqa: E402
from project_context import ProjectContext  # noqa: E402
import gx10  # noqa: E402


def test_exec_cwd_none_without_context() -> None:
    assert pc.current() is None
    assert gx10._exec_cwd() is None


def test_exec_cwd_none_when_root_equals_boot_workdir(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(gx10, "_BOOT_WORKDIR", Path(str(tmp_path)))
    ctx = ProjectContext("p", str(tmp_path), "")
    with pc.use(ctx):
        assert gx10._exec_cwd() is None
    assert pc.current() is None


def test_exec_cwd_returns_root_for_non_default_project(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(gx10, "_BOOT_WORKDIR", Path(str(tmp_path / "boot")))
    ctx = ProjectContext("p", str(tmp_path), "")
    with pc.use(ctx):
        assert gx10._exec_cwd() == str(tmp_path)
    assert pc.current() is None


def test_exec_cwd_none_when_root_empty(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(gx10, "_BOOT_WORKDIR", Path(str(tmp_path / "boot")))
    ctx = ProjectContext("p", "", "")
    with pc.use(ctx):
        assert gx10._exec_cwd() is None
    assert pc.current() is None


def test_resolve_exec_path_absolute_is_unchanged(monkeypatch, tmp_path) -> None:
    abs_path = str(tmp_path / "abs" / "file.txt")
    monkeypatch.setattr(gx10, "_BOOT_WORKDIR", Path(str(tmp_path / "boot")))
    ctx = ProjectContext("p", str(tmp_path), "")
    with pc.use(ctx):
        assert gx10._resolve_exec_path(abs_path) == Path(abs_path)
    assert pc.current() is None


def test_resolve_exec_path_relative_anchors_to_project_root(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(gx10, "_BOOT_WORKDIR", Path(str(tmp_path / "boot")))
    ctx = ProjectContext("p", str(tmp_path), "")
    with pc.use(ctx):
        assert gx10._resolve_exec_path("sub/file.txt") == tmp_path / "sub" / "file.txt"
    assert pc.current() is None


def test_resolve_exec_path_relative_no_context_is_unchanged() -> None:
    assert pc.current() is None
    assert gx10._resolve_exec_path("sub/file.txt") == Path("sub/file.txt")


def test_run_tool_exec_cwd_override_resolves_under_it(tmp_path) -> None:
    # #1317: a bridged client passes the server-shipped active-project exec cwd; run_tool resolves relative
    # file ops there even with NO project bound in the client process (the passthrough case) — not the
    # client's boot workdir.
    gx10._LOCAL_TOOL_BRIDGE = None
    proj = tmp_path / "proj"
    proj.mkdir()
    assert pc.current() is None
    assert "OK" in gx10.run_tool("write_file", {"path": "sub/f.txt", "content": "hi"}, exec_cwd=str(proj))
    assert (proj / "sub" / "f.txt").read_text(encoding="utf-8") == "hi"
    assert gx10.run_tool("read_file", {"path": "sub/f.txt"}, exec_cwd=str(proj)) == "hi"
    assert gx10._exec_cwd() is None                              # the override never leaks past the call


def test_run_tool_exec_cwd_nonexistent_falls_back(monkeypatch, tmp_path) -> None:
    # #1317: an exec cwd that does not exist on THIS host (remote/sealed / older engine) is ignored →
    # byte-identical (a relative path resolves at the process workdir).
    gx10._LOCAL_TOOL_BRIDGE = None
    monkeypatch.chdir(tmp_path)
    assert "OK" in gx10.run_tool("write_file", {"path": "f.txt", "content": "hi"},
                                 exec_cwd=str(tmp_path / "ghost"))
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "hi"


def test_run_tool_write_and_read_file_under_project_root(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(gx10, "_BOOT_WORKDIR", Path(str(tmp_path / "boot")))
    with pc.use(ProjectContext("p", str(tmp_path), "")):
        write_result = gx10.run_tool(
            "write_file", {"path": "sub/f.txt", "content": "hi"}
        )
        assert "OK" in write_result
        assert (tmp_path / "sub" / "f.txt").read_text(encoding="utf-8") == "hi"

        read_result = gx10.run_tool("read_file", {"path": "sub/f.txt"})
        assert read_result == "hi"
    assert pc.current() is None


def test_run_tool_create_directory_under_project_root(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(gx10, "_BOOT_WORKDIR", Path(str(tmp_path / "boot")))
    with pc.use(ProjectContext("p", str(tmp_path), "")):
        result = gx10.run_tool("create_directory", {"path": "d1"})
        assert "OK" in result
        assert (tmp_path / "d1").is_dir()
    assert pc.current() is None


def test_run_tool_list_directory_under_project_root(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(gx10, "_BOOT_WORKDIR", Path(str(tmp_path / "boot")))
    with pc.use(ProjectContext("p", str(tmp_path), "")):
        gx10.run_tool("write_file", {"path": "a.txt", "content": "a"})
        gx10.run_tool("create_directory", {"path": "d1"})
        listing = gx10.run_tool("list_directory", {"path": "."})
        assert "a.txt" in listing
        assert "d1" in listing
    assert pc.current() is None


def test_run_tool_absolute_path_writes_outside_project_root(monkeypatch, tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    abs_file = str(outside / "abs.txt")
    monkeypatch.setattr(gx10, "_BOOT_WORKDIR", Path(str(tmp_path / "boot")))
    with pc.use(ProjectContext("p", str(tmp_path), "")):
        result = gx10.run_tool("write_file", {"path": abs_file, "content": "absolute"})
        assert "OK" in result
        assert (outside / "abs.txt").read_text(encoding="utf-8") == "absolute"
    assert pc.current() is None


def test_run_tool_execute_command_cwd_is_project_root(monkeypatch, tmp_path) -> None:
    recorded: dict[str, object] = {}

    class FakeResult:
        stdout = ""
        stderr = ""
        returncode = 0

    def fake_run(*args, **kwargs) -> FakeResult:
        recorded["cwd"] = kwargs.get("cwd")
        return FakeResult()

    monkeypatch.setattr(gx10, "_BOOT_WORKDIR", Path(str(tmp_path / "boot")))
    monkeypatch.setattr(gx10.subprocess, "run", fake_run)

    with pc.use(ProjectContext("p", str(tmp_path), "")):
        gx10.run_tool("execute_command", {"command": "x"})
        assert recorded["cwd"] == str(tmp_path)
    assert pc.current() is None


def test_run_tool_execute_command_cwd_none_without_context(monkeypatch, tmp_path) -> None:
    recorded: dict[str, object] = {}

    class FakeResult:
        stdout = ""
        stderr = ""
        returncode = 0

    def fake_run(*args, **kwargs) -> FakeResult:
        recorded["cwd"] = kwargs.get("cwd")
        return FakeResult()

    monkeypatch.setattr(gx10, "_BOOT_WORKDIR", Path(str(tmp_path / "boot")))
    monkeypatch.setattr(gx10.subprocess, "run", fake_run)

    assert pc.current() is None
    gx10.run_tool("execute_command", {"command": "x"})
    assert recorded["cwd"] is None


def test_run_tool_move_file_empty_source_is_refused(monkeypatch, tmp_path) -> None:
    """An empty source must be refused, never resolved to '.'/the project root — str(Path('')) == '.'
    so under a project root an empty source would target the root itself (a destructive move)."""
    monkeypatch.setattr(gx10, "_BOOT_WORKDIR", Path(str(tmp_path / "boot")))
    (tmp_path / "keep").mkdir()
    with pc.use(ProjectContext("p", str(tmp_path), "")):
        result = gx10.run_tool("move_file", {"source": "", "destination": "dst"})
        assert "ERROR" in result
        assert tmp_path.is_dir()             # the project root was not moved
        assert (tmp_path / "keep").is_dir()  # nothing under it was disturbed
    assert pc.current() is None


def test_coder_spawn_argument_under_non_default_ctx(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(gx10, "_BOOT_WORKDIR", Path(str(tmp_path / "boot")))
    with pc.use(ProjectContext("p", str(tmp_path), "")):
        assert (gx10._exec_cwd() or ".") == str(tmp_path)
    assert pc.current() is None


def test_coder_spawn_argument_without_context() -> None:
    assert pc.current() is None
    assert (gx10._exec_cwd() or ".") == "."
