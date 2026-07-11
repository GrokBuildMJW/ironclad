"""#1224 (S2) — the BASIC no-blind-advance gate.

Opt-in (default OFF → byte-identical). When on, completion is decided by the feedback file's PRESENCE
(presence-based single authority) — the `status:` token is ADVISORY and may HOLD a finished task ONLY on
an EXPLICIT `blocked`/`clarification_needed`; a present feedback with a done/mis-placed/absent token advances.
The full composed gate stays private.
"""
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


def test_advance_finds_feedback_regardless_of_caller_agent(monkeypatch, tmp_path):
    # Fix 4 (dev-loop stab): the advance keys on the TASK ID and derives the TRUE agent from the matched
    # FILENAME — so a caller-supplied agent skewed by routing (#1287/#1292) still advances, not a permanent miss.
    _setup(monkeypatch, tmp_path)
    tid = _staged(agent="SONNET", status="done")         # feedback is {tid}_SONNET-feedback.md
    out = gx10._advance_pipeline(tid, "OPUS")             # caller passes a DIFFERENT (skewed) configured agent
    assert "not advancing" not in out and "missing" not in out
    assert gx10._store().get(tid)["status"] == "done"     # advanced via the task_id glob + filename-derive


def test_advance_allows_bare_leading_status_feedback(monkeypatch, tmp_path):
    # Fix 1 (dev-loop stab, THE STALL end-to-end): a feedback whose FIRST line is a bare `status: done` (before
    # the `---` fence, exactly as the #1288 coder prompt dictates) now advances instead of stalling in_progress.
    _setup(monkeypatch, tmp_path)
    tid = gx10._store().create(
        {"type": "feature", "priority": "high", "title": "x", "description": "y"}, force=True)["id"]
    gx10._store().transition(tid, "in_progress")
    fb = gx10.feedback_dir() / f"{tid}_OPUS-feedback.md"
    fb.parent.mkdir(parents=True, exist_ok=True)
    fb.write_text("status: done\n---\nfrom: OPUS\n---\n\n## Summary\nok\n", encoding="utf-8")
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


def test_advance_allows_no_status_presence_wins(monkeypatch, tmp_path):
    # Single-authority stab (SF-grade presence-wins): a PRESENT feedback with no recognized status ADVANCES —
    # the token is advisory; only an EXPLICIT blocked/clarification holds. This deletes the whole stall class.
    _setup(monkeypatch, tmp_path)
    tid = _staged(status="")                                     # feedback present, no recognized status token
    out = gx10._advance_pipeline(tid, "OPUS")
    assert "not advancing" not in out
    assert gx10._store().get(tid)["status"] == "done"           # present ⇒ advance


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


def test_feedback_status_parser_leading_or_infence():
    # Dev-loop stab (THE STALL): the coder prompt makes the coder write a BARE leading `status:` line (before
    # the `---` fence). _feedback_status accepts it AND an in-frontmatter status; a status buried deep is not.
    assert gx10._feedback_status("status: done\n---\nfrom: SONNET\n---\n") == "done"   # leading line (stall repro)
    assert gx10._feedback_status("---\nstatus: Done\n---\n") == "done"                 # in-frontmatter, case-insensitive
    assert gx10._feedback_status("---\nstatus: done (green)\n---\n") == "done"         # first token only
    assert gx10._feedback_status("no status here\n") == ""                            # absent
    assert gx10._feedback_status("\n".join(["x"] * 30 + ["status: done"])) == ""      # too deep → not matched


def test_advance_gate_presence_wins_unless_explicit_non_done(monkeypatch):
    # Single-authority stab: a present feedback advances on PRESENCE — a bare leading `status: done`, a
    # mis-placed status, or none at all all advance; ONLY an EXPLICIT blocked/clarification holds.
    monkeypatch.setattr(gx10, "ADVANCE_GATE_ENABLED", True)
    assert gx10._advance_gate("status: done\n---\nfrom: SONNET\n---\n\n## Summary\nok") is None   # bare leading done
    assert gx10._advance_gate("status: blocked\n---\nx\n---\n").startswith("ERROR")               # leading blocked → hold
    assert gx10._advance_gate("\n".join(["l"] * 30 + ["status: done"])) is None                   # no explicit non-done ⇒ advance


def test_advance_gate_unit(monkeypatch):
    monkeypatch.setattr(gx10, "ADVANCE_GATE_ENABLED", True)
    assert gx10._advance_gate("---\nstatus: done\n---\n") is None
    assert gx10._advance_gate("---\nstatus: blocked\n---\n").startswith("ERROR")
    assert gx10._advance_gate("no status here") is None   # presence-wins: no explicit non-done ⇒ advance
    monkeypatch.setattr(gx10, "ADVANCE_GATE_ENABLED", False)
    assert gx10._advance_gate("---\nstatus: blocked\n---\n") is None  # off → allow (byte-identical)


# ── #1346: advance_gate.enabled uses strict _as_bool (string "false" must NOT enable) ──────────────
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, True),
        (False, False),
        ("true", True),
        ("false", False),
        ("0", False),
        ("garbage", False),
        ("", False),
        (1, False),
    ],
)
def test_advance_gate_config_uses_strict_boolean(value, expected):
    gx10._apply_advance_gate({"advance_gate": {"enabled": value}})
    assert gx10.ADVANCE_GATE_ENABLED is expected


def test_advance_gate_config_fails_soft(monkeypatch):
    monkeypatch.setattr(gx10, "_cfg_get", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    gx10._apply_advance_gate({})
    assert gx10.ADVANCE_GATE_ENABLED is False


@pytest.mark.parametrize(
    ("fragment", "expected"),
    [
        ({"advance_gate": {"enabled": True}}, True),
        ({"advance_gate": {"enabled": "true"}}, True),
        ({"advance_gate": {"enabled": "false"}}, False),  # strict _as_bool rejects stringy false
        ({"advance_gate": {"enabled": "garbage"}}, False),
        ({"advance_gate": {"enabled": "0"}}, False),
        ({}, False),  # missing key → public default off
    ],
    ids=["json-true", "string-true", "string-false", "garbage", "string-zero", "missing"],
)
def test_apply_config_advance_gate_synthetic(fragment, expected):
    """#1346: synthetic dict only — string config values must not wrongly enable the gate."""
    cfg = gx10._code_defaults()
    cfg.update(fragment)
    gx10._apply_config(cfg)
    assert gx10.ADVANCE_GATE_ENABLED is expected
