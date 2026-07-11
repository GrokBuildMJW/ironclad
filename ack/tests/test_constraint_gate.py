"""#1339 (S2) — constraint presence-gate + verbatim handover injection.

Fail-closed, opt-in: design-recording / decomposition / implementation handovers are REFUSED until the
active unit has constraints on record. Captured bodies are injected verbatim into every handover
(idempotent strip-then-add, single-snapshot / no TOCTOU). Mirrors the design-gate test harness.
"""
from __future__ import annotations

import json
import re
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
    monkeypatch.setattr(gx10, "DESIGN_GATE_ENABLED", False)  # isolate the constraint gate
    monkeypatch.setattr(gx10, "CONSTRAINT_GATE_ENABLED", gate)
    gx10.STORE = None
    monkeypatch.setattr(gx10, "_ui_print", lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)
    gx10.initiative_new("Demo", "software")


def _impl_json(title="build it"):
    return json.dumps({"type": "implementation", "priority": "high", "title": title, "description": "x"})


def _stage(task_json, body="handover body"):
    return gx10._stage_handover(None, "OPUS", body, task_json)


def _pending():
    return gx10._store().list("pending")


def _handover_text(tid: str, agent: str = "OPUS") -> str:
    return (gx10.handovers_dir() / f"{tid}_{agent}.md").read_text(encoding="utf-8")


def _block_count(md: str) -> int:
    return len(re.findall(r"<!-- IRONCLAD:CONSTRAINTS -->", md))


def test_gate_off_allows_impl_and_injects_no_block(monkeypatch, tmp_path):
    # opt-in: gate DISABLED (default) → implementation handover allowed, no constraint block.
    _setup(monkeypatch, tmp_path, gate=False)
    out = _stage(_impl_json())
    assert "refused" not in out.lower()
    assert "constraints not on record" not in out
    assert len(_pending()) == 1
    md = _handover_text(_pending()[0]["id"])
    assert "<!-- IRONCLAD:CONSTRAINTS -->" not in md
    assert "constraints:" not in gx10._steering_state_block()


def test_impl_refused_without_constraints(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    out = _stage(_impl_json())
    assert "constraints not on record" in out
    assert "record_constraints" in out
    assert _pending() == []  # fail-closed BEFORE store.create


def test_impl_allowed_after_record_constraints(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    gx10.record_constraints("Scope", "stay local")
    out = _stage(_impl_json())
    assert "refused" not in out.lower() and "constraints not on record" not in out
    assert len(_pending()) == 1


def test_force_does_not_bypass_constraint_gate(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    out = gx10._stage_handover(None, "OPUS", "body", _impl_json(), True, True)  # force=True
    assert "constraints not on record" in out
    assert _pending() == []


def test_plan_units_refused_without_constraints_atomic(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    epic = {"type": "epic", "priority": "high", "title": "Epic A", "description": "e"}
    unit = {"type": "implementation", "priority": "high", "title": "U1", "description": "u"}
    out = gx10._plan_units(json.dumps(epic), json.dumps([unit]))
    assert out.startswith("ERROR") and "constraints not on record" in out
    assert gx10._store().list() == []  # atomic — nothing created


def test_record_design_refused_without_constraints(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    doc = gx10.vault_root() / gx10.active_slug() / "decisions" / "design.md"
    with pytest.raises(gx10.GateRefusal) as exc:
        gx10.record_design("Approach", "use Rust")
    assert "constraints not on record" in str(exc.value)
    assert "record_constraints" in str(exc.value)
    assert not doc.exists()
    # dispatch surfaces ERROR: …
    out = gx10.run_tool("record_design", {"title": "Approach", "body": "use Rust"})
    assert out.startswith("ERROR:") and "constraints not on record" in out
    assert not doc.exists()


def test_non_impl_handover_unaffected(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    tj = json.dumps({"type": "architecture", "priority": "high", "title": "design it", "description": "x"})
    out = _stage(tj)
    assert "refused" not in out.lower() and "constraints not on record" not in out
    assert len(_pending()) == 1  # design/analysis handover is NOT presence-gated


def test_verbatim_injection_on_create_and_rehand(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    body = "  hard: no network\nsoft: prefer local  "
    gx10.record_constraints("Scope floor", body, network="none")
    expected = gx10._trim_constraint_body(body)

    out = _stage(_impl_json())
    assert "refused" not in out.lower()
    tid = _pending()[0]["id"]
    md = _handover_text(tid)
    assert _block_count(md) == 1
    assert "<!-- IRONCLAD:CONSTRAINTS -->" in md
    assert "## Constraints (authoritative — honour verbatim; do not override)" in md
    assert expected in md
    assert "<!-- /IRONCLAD:CONSTRAINTS -->" in md
    # body is between markers and byte-preserved
    open_m, close_m = gx10._CONSTRAINT_MARKERS
    inner = md.split(open_m, 1)[1].split(close_m, 1)[0]
    assert expected in inner

    out2 = gx10._stage_handover(tid, "OPUS", "re-handover body", None)
    assert "refused" not in out2.lower()
    md2 = _handover_text(tid)
    assert _block_count(md2) == 1  # re-hand does NOT accumulate a second block
    assert expected in md2


def test_rehand_idempotency_exactly_one_block(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    gx10.record_constraints("Scope", "stay local")
    _stage(_impl_json())
    tid = _pending()[0]["id"]
    stale = (
        "<!-- IRONCLAD:CONSTRAINTS -->\n## Constraints\nold\n"
        "<!-- /IRONCLAD:CONSTRAINTS -->\n\n"
        "<!-- IRONCLAD:CONSTRAINTS -->\n## Constraints\nalso old\n"
        "<!-- /IRONCLAD:CONSTRAINTS -->\n\nhandover body"
    )
    out = gx10._stage_handover(tid, "OPUS", stale, None)
    assert "refused" not in out.lower()
    md = _handover_text(tid)
    assert _block_count(md) == 1
    assert "stay local" in md
    assert "also old" not in md


def test_captured_none_strips_stale_block_injects_nothing(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    gx10.record_constraints("No constraints", "none")
    # CAPTURED_NONE passes the presence gate; a stale injection block in the body is stripped, nothing re-added.
    stale = (
        "<!-- IRONCLAD:CONSTRAINTS -->\n## Constraints\nstale body\n"
        "<!-- /IRONCLAD:CONSTRAINTS -->\n\nhandover body"
    )
    out = _stage(_impl_json(), body=stale)
    assert "refused" not in out.lower()
    assert len(_pending()) == 1
    md = _handover_text(_pending()[0]["id"])
    assert "<!-- IRONCLAD:CONSTRAINTS -->" not in md
    assert "stale body" not in md
    assert "handover body" in md


def test_single_read_spy_no_toctou(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    gx10.record_constraints("Scope", "stay local")
    reads: list = []
    real = gx10._constraint_status

    def spy(slug):
        reads.append(slug)
        return real(slug)

    monkeypatch.setattr(gx10, "_constraint_status", spy)
    out = _stage(_impl_json())
    assert "refused" not in out.lower()
    assert len(reads) == 1  # exactly one snapshot drives gate + injection
    md = _handover_text(_pending()[0]["id"])
    assert "stay local" in md and _block_count(md) == 1

    reads.clear()
    tid = _pending()[0]["id"]
    out2 = gx10._stage_handover(tid, "OPUS", "re-hand", None)
    assert "refused" not in out2.lower()
    assert len(reads) == 1  # re-hand path also single-reads


def test_constraint_gate_unit(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, gate=False)
    slug = gx10.active_slug()
    assert gx10._constraint_gate(slug) is None  # off → None
    assert gx10._constraint_gate(None) is None

    monkeypatch.setattr(gx10, "CONSTRAINT_GATE_ENABLED", True)
    assert gx10._constraint_gate(slug).startswith("ERROR")  # UNCAPTURED
    assert gx10._constraint_gate(None).startswith("ERROR")  # no unit
    assert "record_constraints" in gx10._constraint_gate(slug)

    gx10.record_constraints("Scope", "stay local")
    assert gx10._constraint_gate(slug) is None  # CAPTURED
    snap = gx10._constraint_status(slug)
    assert snap[0] == gx10.CAPTURED
    assert gx10._constraint_gate(slug, snapshot=snap) is None  # pre-read snapshot accepted

    gx10.record_constraints("None", "none")
    assert gx10._constraint_gate(slug) is None  # CAPTURED_NONE
    none_snap = (gx10.UNCAPTURED, None)
    assert gx10._constraint_gate(slug, snapshot=none_snap).startswith("ERROR")  # snapshot wins


def test_steering_gate_semantics(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    block = gx10._steering_state_block()
    assert ("constraints: NOT on record — design/decomposition/implementation handovers BLOCKED "
            "(call record_constraints)") in block

    gx10.record_constraints("Scope", "stay local")
    assert "constraints: on record — OK" in gx10._steering_state_block()

    gx10.record_constraints("None", "none")
    assert "constraints: none on record — OK" in gx10._steering_state_block()


def test_rehandover_of_impl_task_gated_when_uncaptured(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    gx10.record_constraints("Scope", "stay local")
    _stage(_impl_json())
    tid = _pending()[0]["id"]
    # Wipe constraints so re-hand of an impl task must re-check the gate.
    doc = gx10.vault_root() / gx10.active_slug() / "decisions" / "constraints.md"
    doc.unlink()
    out = gx10._stage_handover(tid, "OPUS", "impl now", None)
    assert "constraints not on record" in out
