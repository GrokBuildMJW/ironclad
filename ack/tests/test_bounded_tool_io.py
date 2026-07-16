"""#1488: model-facing filesystem tools bound I/O before allocating/decoding it."""
from __future__ import annotations

import sys
import types
from pathlib import Path

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=object))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402


def test_read_file_refuses_sparse_file_above_byte_bound_without_read_text(tmp_path, monkeypatch):
    target = tmp_path / "oversized.txt"
    with target.open("wb") as fh:
        fh.seek(gx10._MAX_FILE_BYTES)
        fh.write(b"x")

    def _unbounded_read_forbidden(*_args, **_kwargs):
        raise AssertionError("read_file must not call Path.read_text")

    monkeypatch.setattr(Path, "read_text", _unbounded_read_forbidden)
    out = gx10.run_tool("read_file", {"path": str(target)})

    assert "file too large" in out
    assert f"{gx10._MAX_FILE_BYTES + 1} bytes" in out
    assert f"cap {gx10._MAX_FILE_BYTES} bytes" in out


def test_edit_file_refuses_sparse_file_above_byte_bound_without_read_text(tmp_path, monkeypatch):
    target = tmp_path / "oversized.txt"
    with target.open("wb") as fh:
        fh.seek(gx10._MAX_FILE_BYTES)
        fh.write(b"x")

    def _unbounded_read_forbidden(*_args, **_kwargs):
        raise AssertionError("edit_file must not call Path.read_text")

    monkeypatch.setattr(Path, "read_text", _unbounded_read_forbidden)
    out = gx10.run_tool(
        "edit_file", {"path": str(target), "old_string": "x", "new_string": "y"}
    )

    assert "edit_file refused" in out
    assert "file too large" in out
    assert f"{gx10._MAX_FILE_BYTES + 1} bytes" in out
    assert f"cap {gx10._MAX_FILE_BYTES} bytes" in out


def test_review_paths_refuses_sparse_file_above_byte_bound_without_read_text(tmp_path, monkeypatch):
    oversized = tmp_path / "oversized.txt"
    with oversized.open("wb") as fh:
        fh.seek(gx10._MAX_FILE_BYTES)
        fh.write(b"x")
    readable = tmp_path / "readable.txt"
    readable.write_bytes(b"bounded review material\n")

    def _unbounded_read_forbidden(*_args, **_kwargs):
        raise AssertionError("review paths must not call Path.read_text")

    monkeypatch.setattr(Path, "read_text", _unbounded_read_forbidden)
    monkeypatch.setattr(gx10, "_resolve_exec_path", lambda path: tmp_path / path)
    mode, material = gx10._assemble_review_material(["oversized.txt", "readable.txt"])

    assert mode == "paths"
    assert "file too large" in material
    assert f"{gx10._MAX_FILE_BYTES + 1} bytes" in material
    assert f"cap {gx10._MAX_FILE_BYTES} bytes" in material
    assert "bounded review material" in material


def test_vault_docs_skips_sparse_file_above_byte_bound_without_read_text(tmp_path, monkeypatch):
    oversized = tmp_path / "oversized.md"
    with oversized.open("wb") as fh:
        fh.seek(gx10._MAX_FILE_BYTES)
        fh.write(b"x")
    readable = tmp_path / "readable.md"
    readable.write_bytes(b"# Bounded vault document\n")

    def _unbounded_read_forbidden(*_args, **_kwargs):
        raise AssertionError("vault document scans must not call Path.read_text")

    monkeypatch.setattr(Path, "read_text", _unbounded_read_forbidden)
    docs = gx10._vault_docs(tmp_path)

    assert [doc["rel"] for doc in docs] == [Path("readable.md")]
    assert docs[0]["title"] == "Bounded vault document"


def test_list_directory_stops_after_cap_plus_one_without_materializing_all(tmp_path, monkeypatch):
    class _Entry:
        def __init__(self, n):
            self.name = f"f{n:04d}.txt"

        def is_dir(self):
            return False

        def is_file(self):
            return True

        def stat(self):
            return types.SimpleNamespace(st_mtime=0)

    def _bounded_entries(_self):
        for i in range(gx10.LIST_DIR_HARD_CAP + 1):
            yield _Entry(i)
        raise AssertionError("list_directory consumed beyond its cap-plus-one probe")

    monkeypatch.setattr(Path, "iterdir", _bounded_entries)
    out = gx10._run_tool_dispatch_impl("list_directory", {"path": str(tmp_path)})

    assert len(out.splitlines()) == gx10.LIST_DIR_HARD_CAP + 2
    assert f"first {gx10.LIST_DIR_HARD_CAP} entries (filesystem order) of many" in out
    assert "partial sample, not the whole directory" in out   # #1488 M1: honest overflow note
    assert f"[F] f{gx10.LIST_DIR_HARD_CAP:04d}.txt" not in out


def test_search_files_short_circuits_at_hit_cap(tmp_path, monkeypatch):
    candidate = tmp_path / "hit.md"
    candidate.write_text("needle\n", encoding="utf-8")

    class _Root:
        def rglob(self, _pattern):
            for _ in range(gx10._SEARCH_HIT_CAP):
                yield candidate
            raise AssertionError("search_files walked after reaching its hit cap")

    monkeypatch.setattr(gx10, "_resolve_exec_path", lambda _path: _Root())
    out = gx10._run_tool_dispatch_impl(
        "search_files", {"pattern": "needle", "directory": ".", "file_pattern": "*.md"}
    )

    assert len(out.splitlines()) == gx10._SEARCH_HIT_CAP + 1
    assert f"stopped at the {gx10._SEARCH_HIT_CAP}-hit cap" in out


def test_search_files_stops_before_reading_past_file_budget(tmp_path, monkeypatch):
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    beyond = tmp_path / "beyond.md"
    first.write_text("no match", encoding="utf-8")
    second.write_text("still no match", encoding="utf-8")
    beyond.write_text("needle", encoding="utf-8")

    class _Root:
        def rglob(self, _pattern):
            yield first
            yield second
            yield beyond

    monkeypatch.setattr(gx10, "_SEARCH_MAX_FILES", 2)
    monkeypatch.setattr(gx10, "_resolve_exec_path", lambda _path: _Root())
    out = gx10._run_tool_dispatch_impl(
        "search_files", {"pattern": "needle", "directory": ".", "file_pattern": "*.md"}
    )

    assert out.startswith("No matches")
    assert "stopped after the 2-file scan budget" in out
    assert str(beyond) not in out
