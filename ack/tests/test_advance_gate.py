"""#1224 (S2) — the BASIC no-blind-advance gate.

Opt-in (default OFF → byte-identical). When on, an advance to `done` requires a feedback `status: done`;
`blocked`/`clarification_needed`/no-status is refused ("no signal ≠ done", fail-closed). The full composed
gate stays private.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=lambda **kw: object()))
_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402


def _setup(monkeypatch, tmp_path, *, gate=True):
    gx10._apply_config(gx10._code_defaults())
    monkeypatch.setattr(gx10, "ADVANCE_GATE_ENABLED", gate)   # opt-in; _apply_config reset it to off
    gx10.STORE = None
    monkeypatch.setattr(gx10, "_ui_print", lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)
    gx10.initiative_new("Demo", "software")


def _staged(agent="OPUS", status="done"):
    """An in_progress task + a feedback file carrying the given status ('' → no frontmatter status)."""
    tid = gx10._store().create(
        {"type": "feature", "priority": "high", "title": "x", "description": "y"}, force=True)["id"]
    gx10._store().transition(tid, "in_progress")
    fb = gx10.feedback_dir() / f"{tid}_{agent}-feedback.md"
    fb.parent.mkdir(parents=True, exist_ok=True)
    fb.write_text(f"---\nstatus: {status}\n---\ndone\n" if status else "no frontmatter here\n", encoding="utf-8")
    return tid


def test_advance_allows_done(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    tid = _staged(status="done")
    out = gx10._advance_pipeline(tid, "OPUS")
    assert "not advancing" not in out
    assert gx10._store().get(tid)["status"] == "done"


def test_advance_refuses_blocked(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    tid = _staged(status="blocked")
    out = gx10._advance_pipeline(tid, "OPUS")
    assert "not advancing" in out and "blocked" in out
    assert gx10._store().get(tid)["status"] == "in_progress"     # fail-closed — not advanced


def test_advance_refuses_clarification(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    tid = _staged(status="clarification_needed")
    out = gx10._advance_pipeline(tid, "OPUS")
    assert "not advancing" in out
    assert gx10._store().get(tid)["status"] == "in_progress"


def test_advance_refuses_no_status(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    tid = _staged(status="")                                     # feedback with no recognized status
    out = gx10._advance_pipeline(tid, "OPUS")
    assert "not advancing" in out and "no recognized completion status" in out
    assert gx10._store().get(tid)["status"] == "in_progress"


def test_advance_gate_off_is_byte_identical(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, gate=False)                   # gate OFF (the default)
    tid = _staged(status="blocked")                            # a blocked feedback — but the gate is off
    out = gx10._advance_pipeline(tid, "OPUS")
    assert "not advancing" not in out
    assert gx10._store().get(tid)["status"] == "done"          # advances on feedback presence (old behavior)


def test_advance_gate_tolerant_parsing(monkeypatch):
    # Sonnet: case-insensitive KEY + first-token value → a slightly-off but genuine `done` still advances,
    # while a `blocked (reason)` is still refused. Prevents a real completion from getting stuck in_progress.
    monkeypatch.setattr(gx10, "ADVANCE_GATE_ENABLED", True)
    assert gx10._advance_gate("---\nStatus: DONE\n---\n") is None                   # capital key + upper value
    assert gx10._advance_gate("---\nstatus: done (all tests green)\n---\n") is None  # trailing text on value
    assert gx10._advance_gate("---\nstatus: blocked (needs infra)\n---\n").startswith("ERROR")  # blocked + reason


def test_advance_gate_unit(monkeypatch):
    monkeypatch.setattr(gx10, "ADVANCE_GATE_ENABLED", True)
    assert gx10._advance_gate("---\nstatus: done\n---\n") is None
    assert gx10._advance_gate("---\nstatus: blocked\n---\n").startswith("ERROR")
    assert gx10._advance_gate("no status here").startswith("ERROR")   # 'no signal ≠ done'
    monkeypatch.setattr(gx10, "ADVANCE_GATE_ENABLED", False)
    assert gx10._advance_gate("---\nstatus: blocked\n---\n") is None  # off → allow (byte-identical)
