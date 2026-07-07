"""#1225 (S3) — per-turn AUTHORITATIVE steering-state injection into the orchestrator model.

The builder reads active project · unit · lifecycle stage · pending/in_progress counts · watcher/autopilot
from the SAME globals the plumbing acts on, folds a compact block onto the user turn (after the stable system
prefix, KV-cache-safe), and returns "" when nothing is bound so a plain-chat turn stays byte-identical. It
must never raise. These tests cover the builder (state → string) and the run() injection, modelled on
``test_context_rag.py``.
"""
from __future__ import annotations

import json

import gx10


class _FakeStore:
    """Minimal TaskStore stand-in: .list(status) → a canned list of the given length."""

    def __init__(self, pending: int = 0, in_progress: int = 0):
        self._counts = {"pending": pending, "in_progress": in_progress}

    def list(self, status=None):
        return [{"id": f"KGC-{i}"} for i in range(self._counts.get(status, 0))]


def _bind(monkeypatch, *, project="dev1test", status="ok", unit="my-unit",
          pending=1, in_progress=2, watcher=True, autopilot=False):
    """Monkeypatch every state source so the builder sees a fully-bound state."""
    monkeypatch.setattr(gx10, "registry_health",
                        lambda: {"status": status, "active_project": project, "home": None})
    monkeypatch.setattr(gx10, "active_slug", lambda: unit)
    monkeypatch.setattr(gx10, "_store", lambda: _FakeStore(pending, in_progress))
    monkeypatch.setattr(gx10, "_WATCHER_ENABLED", watcher)
    monkeypatch.setattr(gx10, "AUTOPILOT_ENABLED", autopilot)


# ── builder: state → string ──────────────────────────────────────────────────

def test_block_full_state(monkeypatch):
    _bind(monkeypatch)
    block = gx10._steering_state_block()
    assert block.startswith(gx10._STEERING_MARKER)
    assert "active project: dev1test" in block
    assert "active unit (initiative): my-unit" in block
    assert "1 pending" in block and "2 in_progress" in block
    assert "watcher: on" in block and "autopilot: off" in block
    assert "do NOT invent a vault path" in block


def test_block_empty_when_nothing_bound(monkeypatch):
    # no project AND no unit → "" so the plain-chat / unisolated turn is byte-identical
    _bind(monkeypatch, project=None, status="unisolated", unit=None)
    assert gx10._steering_state_block() == ""


def test_block_unisolated_but_unit_active(monkeypatch):
    _bind(monkeypatch, project=None, status="unisolated", unit="u")
    block = gx10._steering_state_block()
    assert block  # present because a unit is active
    assert "un-isolated" in block
    assert "active unit (initiative): u" in block


def test_block_failsoft_when_store_raises(monkeypatch):
    _bind(monkeypatch)

    def _boom():
        raise RuntimeError("store down")

    monkeypatch.setattr(gx10, "_store", _boom)
    block = gx10._steering_state_block()          # must NOT raise
    assert block.startswith(gx10._STEERING_MARKER)
    assert "0 pending" in block and "0 in_progress" in block


def test_block_failsoft_when_active_slug_raises(monkeypatch):
    _bind(monkeypatch)

    def _boom():
        raise RuntimeError("slug read failed")

    monkeypatch.setattr(gx10, "active_slug", _boom)
    # a hint must never break a turn → the blanket guard returns ""
    assert gx10._steering_state_block() == ""


def test_block_stage_from_cached_graph(monkeypatch, tmp_path):
    _bind(monkeypatch, unit="my-unit")
    monkeypatch.setattr(gx10, "vault_root", lambda: tmp_path)
    unit_dir = tmp_path / "my-unit"
    unit_dir.mkdir()
    graph = unit_dir / gx10.GRAPH_FILENAME
    graph.write_text(json.dumps({"lifecycle": {"current": "spec"}}), encoding="utf-8")
    assert "lifecycle stage: spec" in gx10._steering_state_block()
    graph.unlink()                                # no cached projection → stage line omitted
    assert "lifecycle stage:" not in gx10._steering_state_block()


# ── injection into run() (modelled on test_context_rag.test_run_flag_*) ───────

def _mk_agent(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(gx10, "OpenAI", lambda **kw: object())
    g = gx10.GX10(base_url="http://x/v1", api_key="k", model="m", prompt_path="")
    g.messages = [{"role": "system", "content": "SYS"}]
    return g


def _steering_msgs(g):
    return [m for m in g.messages if str(m.get("content", "")).startswith(gx10._STEERING_MARKER)]


def test_run_injects_single_steering_message(monkeypatch, tmp_path):
    monkeypatch.setattr(gx10, "RAG_ENABLED", False)
    _bind(monkeypatch)
    g = _mk_agent(monkeypatch, tmp_path)
    g._generate = lambda think: ("ok", [], False, None, None)   # end the loop in one iteration
    g.run("do the thing")
    steering = _steering_msgs(g)
    assert len(steering) == 1                                   # exactly one steering message
    assert "active project: dev1test" in steering[0]["content"]
    last_user = [m for m in g.messages if m.get("role") == "user"][-1]
    assert last_user["content"] == "do the thing"              # the user turn itself stays verbatim


def test_run_keeps_single_copy_after_state_change(monkeypatch, tmp_path):
    # Codex finding #1: an authoritative block must not accumulate stale copies across a project/unit switch.
    monkeypatch.setattr(gx10, "RAG_ENABLED", False)
    g = _mk_agent(monkeypatch, tmp_path)
    g._generate = lambda think: ("ok", [], False, None, None)
    _bind(monkeypatch, project="alpha", unit="unit-a")
    g.run("turn one")
    _bind(monkeypatch, project="beta", unit="unit-b")          # state switches between turns
    g.run("turn two")
    steering = _steering_msgs(g)
    assert len(steering) == 1                                  # no stale accumulation
    assert "active project: beta" in steering[0]["content"]    # reflects the CURRENT state only
    assert "alpha" not in steering[0]["content"]


def test_run_byte_identical_when_unbound(monkeypatch, tmp_path):
    monkeypatch.setattr(gx10, "RAG_ENABLED", False)
    _bind(monkeypatch, project=None, status="unisolated", unit=None)
    g = _mk_agent(monkeypatch, tmp_path)
    g._generate = lambda think: ("ok", [], False, None, None)
    g.run("hello world")
    last_user = [m for m in g.messages if m.get("role") == "user"][-1]
    assert last_user["content"] == "hello world"                # nothing bound → verbatim
