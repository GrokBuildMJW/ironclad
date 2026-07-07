"""#1228 (S6 / R5) — the central task board: BOARD.md, an LLM-free projection of the TaskStore.

All units grouped pending/in_progress/done — the operator's steering view. Deterministic (timestamp-free →
idempotent), rendered on demand via /board and kept current via a fail-soft soft-reconcile backstop, and
excluded from the vault index/graph so it never self-pollutes.
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=lambda **kw: object()))
_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402


def _setup(monkeypatch, tmp_path, *, unit=True):
    gx10._apply_config(gx10._code_defaults())
    gx10.STORE = None
    monkeypatch.setattr(gx10, "_ui_print", lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)
    if unit:
        gx10.initiative_new("Demo", "software")


def _mk(title, status=None, **extra):
    t = gx10._store().create(
        {"type": "feature", "priority": "high", "title": title, "description": "x", **extra}, force=True)
    if status and status != "pending":
        gx10._store().transition(t["id"], status)
    return t["id"]


def test_render_board_groups_by_status(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _mk("stays pending")
    _mk("now running", status="in_progress")
    _mk("all done", status="done", labels=["area/ci"], parent="KGC-1")
    b = gx10._render_board(gx10.active_slug())
    assert gx10._BOARD_AUTO_START in b and gx10._BOARD_AUTO_END in b
    assert "## pending (1)" in b and "## in_progress (1)" in b and "## done (1)" in b
    assert "stays pending" in b and "now running" in b and "all done" in b
    assert "labels: area/ci" in b and "parent: KGC-1" in b


def test_board_command_writes_and_displays(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _mk("a task")
    out = gx10._board_command(None)
    assert "## pending (1)" in out and "a task" in out
    board = gx10.vault_root() / gx10.active_slug() / gx10.BOARD_FILENAME
    assert board.is_file() and gx10._BOARD_AUTO_START in board.read_text(encoding="utf-8")


def test_board_idempotent(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _mk("x")
    assert gx10._render_board(gx10.active_slug()) == gx10._render_board(gx10.active_slug())  # timestamp-free
    gx10._write_board()
    f = gx10.vault_root() / gx10.active_slug() / gx10.BOARD_FILENAME
    first = f.read_text(encoding="utf-8")
    gx10._write_board()
    assert f.read_text(encoding="utf-8") == first          # a second write changes nothing


def test_board_no_active_unit_is_friendly(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, unit=False)              # no active unit
    out = gx10._board_command(None)
    assert "No active unit" in out


def test_board_excluded_from_index_and_graph(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _mk("x")
    gx10._write_board()
    gx10.reconcile_vault(gx10.active_slug())
    idx = (gx10.vault_root() / gx10.active_slug() / "INDEX.md").read_text(encoding="utf-8")
    assert "BOARD" not in idx                               # not self-indexed
    graph = json.loads((gx10.vault_root() / gx10.active_slug() / gx10.GRAPH_FILENAME).read_text(encoding="utf-8"))
    assert not any("BOARD" in str(k) for k in (graph.get("nodes") or {}))


def test_board_survives_malformed_task(monkeypatch, tmp_path):
    # Sonnet finding #1: a task stored with non-string fields (possible when the ACK contract is soft) must
    # RENDER, not crash /board with a str.join TypeError.
    _setup(monkeypatch, tmp_path)
    gx10._store().create(
        {"type": 7, "priority": "high", "title": "bad", "description": "x", "labels": [1, 2]}, force=True)
    out = gx10._board_command(None)                          # must not raise
    assert "bad" in out and "7" in out and "1, 2" in out


def test_board_autoregen_on_stage_handover(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)                          # design_gate off by default → feature not blocked
    tj = json.dumps({"type": "feature", "priority": "high", "title": "wired task", "description": "y"})
    gx10._stage_handover(None, "OPUS", "handover", tj)     # → _reconcile_active_soft → _write_board (backstop)
    board = gx10.vault_root() / gx10.active_slug() / gx10.BOARD_FILENAME
    assert board.is_file() and "wired task" in board.read_text(encoding="utf-8")
