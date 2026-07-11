"""#1359 (S7 / epic #1344) — L2/L3 capstone: fork → decide → hard-check + gate-off byte-identical.

Integration proof for the full constraint-compliance flow through REAL ``run_tool`` /
command dispatch: L1 capture → L2 detect (fork envelope) → pending-fork block on
``/approve design`` → ``/fork decide`` keep|counter → L3 hard-check at approve +
impl ``stage_handover``, plus both flags off remain byte-identical to pre-#1344.

Test-only capstone — no engine changes. MPR worker is NOT required
(``recommendation: None`` is fine). Reuses the ``test_constraints`` /
``test_fork_surface`` harness (openai stub, engine sys.path, ``_apply_config``,
chdir ``tmp_path``, ``initiative_new``).
"""
from __future__ import annotations

import json
import re
import sys
import types
from pathlib import Path

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=lambda **kw: object()))
_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402

# Pre-S4 empty `/fork` fall-through (exact string when both L2 flags leave no ledger).
_PRE_S4_FORK_EMPTY = (
    "No pending MPR fork proposals. When an architecture fork is declared and the gate "
    "`ace.fork_mpr.enabled` is on, its decision-matrix appears here as a recommendation."
)


def _setup(monkeypatch, tmp_path, *, gate=False, detect=False):
    gx10._apply_config(gx10._code_defaults())
    monkeypatch.setattr(gx10, "DESIGN_GATE_ENABLED", False)  # isolate constraint L2/L3
    monkeypatch.setattr(gx10, "CONSTRAINT_GATE_ENABLED", gate)
    monkeypatch.setattr(gx10, "CONSTRAINT_CONFLICT_DETECT", detect)
    gx10.STORE = None
    monkeypatch.setattr(gx10, "_ui_print", lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)
    gx10.initiative_new("Demo", "software")


def _slug() -> str:
    return gx10.active_slug()


def _impl_json(title: str = "build it", **typed) -> str:
    payload = {
        "type": "implementation",
        "priority": "high",
        "title": title,
        "description": "x",
    }
    payload.update(typed)
    return json.dumps(payload)


def _pending():
    return gx10._store().list("pending")


def _constraints_doc() -> Path:
    return gx10.vault_root() / _slug() / "decisions" / "constraints.md"


def _design_doc() -> Path:
    return gx10.vault_root() / _slug() / "decisions" / "design.md"


def _forks_dir() -> Path:
    return gx10.vault_root() / _slug() / "proposals" / "forks"


def _handover_path(tid: str) -> Path:
    matches = list(gx10.handovers_dir().glob(f"{tid}_*.md"))
    assert len(matches) == 1, f"expected one handover for {tid}, found {matches}"
    return matches[0]


def _handover_text(tid: str) -> str:
    return _handover_path(tid).read_text(encoding="utf-8")


def _capture_python(constraint_body: str = "python only") -> str:
    """record_constraints via real dispatch → CAPTURED + HARD typed language=python."""
    cap = gx10.run_tool(
        "record_constraints",
        {"title": "C", "body": constraint_body, "language": "python"},
    )
    assert cap.startswith("OK: constraints recorded at ") and cap.endswith("(CAPTURED).")
    assert gx10._constraint_typed(_slug()) == {"language": "python"}
    return constraint_body


def _record_design_rust() -> str:
    """record_design via real dispatch with language=rust → OK + pending language fork."""
    out = gx10.run_tool(
        "record_design",
        {"title": "D", "body": "use rust", "language": "rust"},
    )
    assert out.startswith("OK: design proposal recorded at ")
    loaded = gx10._load_fork_envelopes(_slug())
    assert len(loaded) == 1
    env = loaded[0]
    assert env.status == "pending"
    assert env.category == "language"
    opt_ids = [str(o.get("id", "")).strip().lower() for o in (env.options or [])]
    assert "keep" in opt_ids and "counter" in opt_ids
    assert env.recommendation is None  # MPR worker not required
    assert env.counter_design
    assert "#" not in env.fork_id  # opaque id
    return env.fork_id


def _assert_approve_blocked_pending(fork_id: str) -> None:
    out = gx10._approve_design()
    assert out.startswith("ERROR")
    assert "pending constraint fork" in out.lower()
    assert fork_id in out
    assert f"/fork decide {fork_id} --choice keep" in out
    assert f"/fork decide {fork_id} --choice counter" in out
    assert "<fork-id>" not in out
    text = _design_doc().read_text(encoding="utf-8")
    assert "approved: true" not in text.replace(" ", "").lower()
    fm = gx10._parse_frontmatter(text)
    assert fm.get("approved") != "true"


# --------------------------------------------------------------------------- #
# Test 1 — full flow, KEEP path (gate on)
# --------------------------------------------------------------------------- #


def test_l2l3_keep_path_full_flow(monkeypatch, tmp_path):
    """Capture → conflict fork → list → approve blocked → keep → hard-refuse → rerecord → OK."""
    _setup(monkeypatch, tmp_path, gate=True, detect=True)
    constraint_body = _capture_python("python only")

    # 2) Conflicting design records a pending language fork (L2 detect).
    fork_id = _record_design_rust()

    # 3) /fork list renders the pending fork (opaque id, keep/counter).
    listed = gx10._fork_command("")
    assert fork_id in listed
    assert "keep" in listed and "counter" in listed
    assert f"/fork decide {fork_id} --choice keep" in listed
    assert f"/fork decide {fork_id} --choice counter" in listed
    assert "<fork-id>" not in listed
    assert f"#{fork_id}" not in listed
    assert fork_id in gx10._fork_command("list")

    # 4) /approve design REFUSED while the fork is pending.
    _assert_approve_blocked_pending(fork_id)

    # 5) keep → resolved; constraints.md language stays python.
    decide = gx10._fork_decide(fork_id, "keep")
    assert decide.startswith("OK")
    assert "keep" in decide.lower()
    loaded = gx10._load_fork_envelopes(_slug())
    assert len(loaded) == 1
    assert loaded[0].status == "resolved"
    assert (loaded[0].resolution or {}).get("choice_id") == "keep"
    assert gx10._constraint_typed(_slug()) == {"language": "python"}

    # 6) keep clears the rejected counter; approval now asks for a fresh compliant design.
    hard = gx10._approve_design()
    assert hard.startswith("ERROR")
    assert "no design to approve" in hard.lower()
    assert "record one first" in hard.lower()
    assert not _design_doc().exists()

    # 7) Re-record design to match floor → approve OK → impl handover injects constraints.
    re_out = gx10.run_tool(
        "record_design",
        {"title": "D", "body": "use python", "language": "python"},
    )
    assert re_out.startswith("OK: design proposal recorded at ")
    # Matching typed fields → no new pending conflict fork.
    pending_forks = [e for e in gx10._load_fork_envelopes(_slug()) if e.status == "pending"]
    assert pending_forks == []

    approve = gx10._approve_design()
    assert approve.startswith("OK")
    fm = gx10._parse_frontmatter(_design_doc().read_text(encoding="utf-8"))
    assert fm.get("type") == "decision" and fm.get("approved") == "true"

    coder_body = "coder body: implement under python floor"
    stage = gx10.run_tool(
        "stage_handover",
        {
            "agent": "OPUS",
            "handover_md": coder_body,
            "task_json": _impl_json("build it", language="python"),
        },
    )
    assert stage.startswith("OK")
    assert "refused" not in stage.lower()
    assert len(_pending()) == 1
    tid = _pending()[0]["id"]
    md = _handover_text(tid)
    open_m, close_m = gx10._CONSTRAINT_MARKERS
    assert open_m in md and close_m in md
    assert len(re.findall(r"<!-- IRONCLAD:CONSTRAINTS -->", md)) == 1
    inner = md.split(open_m, 1)[1].split(close_m, 1)[0]
    assert constraint_body in inner  # L1 verbatim injection
    assert coder_body in md.split(close_m, 1)[1]


def test_l2l3_keep_reconciles_design_before_resolving_envelope(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, gate=True, detect=True)
    _capture_python("python only")
    fork_id = _record_design_rust()
    real_persist = gx10._persist_fork_envelope
    resolved_design_exists = []

    def inspect_persist(env):
        if getattr(env, "status", "") == "resolved":
            resolved_design_exists.append(_design_doc().exists())
        return real_persist(env)

    monkeypatch.setattr(gx10, "_persist_fork_envelope", inspect_persist)

    decide = gx10._fork_decide(fork_id, "keep")

    assert decide.startswith("OK")
    assert resolved_design_exists == [False]
    assert not _design_doc().exists()


# --------------------------------------------------------------------------- #
# Test 2 — COUNTER path
# --------------------------------------------------------------------------- #


def test_l2l3_counter_path_override_then_approve(monkeypatch, tmp_path):
    """Capture → fork → approve blocked → counter override → approve OK → rust handover OK."""
    _setup(monkeypatch, tmp_path, gate=True, detect=True)
    _capture_python("python only")
    fork_id = _record_design_rust()
    _assert_approve_blocked_pending(fork_id)

    decide = gx10._fork_decide(fork_id, "counter")
    assert decide.startswith("OK")
    assert "counter" in decide.lower()
    assert "operator-override" in decide
    loaded = gx10._load_fork_envelopes(_slug())
    assert loaded[0].status == "resolved"
    assert (loaded[0].resolution or {}).get("choice_id") == "counter"
    assert loaded[0].counter_design
    assert gx10._constraint_typed(_slug()) == {"language": "rust"}
    fm_d = gx10._parse_frontmatter(_design_doc().read_text(encoding="utf-8"))
    assert str(fm_d.get("language") or "").strip().lower() == "rust"
    assert fm_d.get("approved") == "false"
    fm_c = gx10._parse_frontmatter(_constraints_doc().read_text(encoding="utf-8"))
    assert str(fm_c.get("source_language") or "").strip().lower() == "operator-override"
    assert str(fm_c.get("language") or "").strip().lower() == "rust"

    approve = gx10._approve_design()
    assert approve.startswith("OK")
    fm = gx10._parse_frontmatter(_design_doc().read_text(encoding="utf-8"))
    assert fm.get("approved") == "true" and fm.get("type") == "decision"

    stage = gx10.run_tool(
        "stage_handover",
        {
            "agent": "OPUS",
            "handover_md": "coder body under rust floor",
            "task_json": _impl_json("build rust", language="rust"),
        },
    )
    assert stage.startswith("OK")
    assert "refused" not in stage.lower()
    assert len(_pending()) == 1


def test_l2l3_keep_restores_prior_compliant_approved_design(monkeypatch, tmp_path):
    """keep restores the pre-overwrite compliant design body byte-for-byte when available."""
    _setup(monkeypatch, tmp_path, gate=True, detect=True)
    _capture_python("python only")
    assert gx10.run_tool(
        "record_design",
        {"title": "D", "body": "use python", "language": "python"},
    ).startswith("OK")
    assert gx10._approve_design().startswith("OK")
    prior = _design_doc().read_text(encoding="utf-8")

    fork_id = _record_design_rust()
    decide = gx10._fork_decide(fork_id, "keep")
    assert decide.startswith("OK")
    assert "restored the prior floor-compliant design" in decide
    assert _design_doc().read_text(encoding="utf-8") == prior
    assert gx10._constraint_typed(_slug()) == {"language": "python"}
    assert "already approved" in gx10._approve_design().lower()


def test_l2l3_keep_leaves_intervening_compliant_design_untouched(monkeypatch, tmp_path):
    """If the operator re-records a compliant design before deciding, keep does not clear it."""
    _setup(monkeypatch, tmp_path, gate=True, detect=True)
    _capture_python("python only")
    fork_id = _record_design_rust()
    assert gx10.run_tool(
        "record_design",
        {"title": "D2", "body": "use python now", "language": "python"},
    ).startswith("OK")
    compliant = _design_doc().read_text(encoding="utf-8")

    decide = gx10._fork_decide(fork_id, "keep")
    assert decide.startswith("OK")
    assert "already complies" in decide
    assert _design_doc().read_text(encoding="utf-8") == compliant
    assert gx10._constraint_typed(_slug()) == {"language": "python"}


def test_l2l3_migrated_envelope_keep_clears_counter_and_counter_approves(monkeypatch, tmp_path):
    """Old envelopes without parked bodies keep clearing and counter falling back to live design."""
    _setup(monkeypatch, tmp_path, gate=True, detect=True)
    _capture_python("python only")
    fork_id = _record_design_rust()
    path = _forks_dir() / f"{fork_id}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.pop("counter_design", None)
    data.pop("restore_design", None)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    keep = gx10._fork_decide(fork_id, "keep")
    assert keep.startswith("OK")
    assert "cleared the rejected counter-proposal" in keep
    assert not _design_doc().exists()

    counter_tmp = tmp_path / "counter"
    counter_tmp.mkdir()
    _setup(monkeypatch, counter_tmp, gate=True, detect=True)
    _capture_python("python only")
    fork_id = _record_design_rust()
    path = _forks_dir() / f"{fork_id}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.pop("counter_design", None)
    data.pop("restore_design", None)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    counter = gx10._fork_decide(fork_id, "counter")
    assert counter.startswith("OK")
    assert gx10._constraint_typed(_slug()) == {"language": "rust"}
    assert gx10._approve_design().startswith("OK")


# --------------------------------------------------------------------------- #
# Test 3 — omission fail-closed (#25)
# --------------------------------------------------------------------------- #


def test_l2l3_omission_refuses_impl_handover(monkeypatch, tmp_path):
    """HARD language=python + task_json omits language → REFUSED before any write."""
    _setup(monkeypatch, tmp_path, gate=True, detect=True)
    _capture_python("python only")
    # Design matching the floor so only the L3 omission path is under test.
    assert gx10.run_tool(
        "record_design",
        {"title": "D", "body": "use python", "language": "python"},
    ).startswith("OK")
    assert gx10._approve_design().startswith("OK")

    out = gx10.run_tool(
        "stage_handover",
        {
            "agent": "OPUS",
            "handover_md": "coder body missing language",
            "task_json": _impl_json("no lang"),  # language omitted
        },
    )
    assert out.startswith("ERROR")
    assert "language" in out.lower()
    assert "missing" in out.lower() or "declare" in out.lower()
    assert _pending() == []
    assert not list(gx10.handovers_dir().glob("*.md"))


# --------------------------------------------------------------------------- #
# Test 4 — gate-off byte-identical (both flags off)
# --------------------------------------------------------------------------- #


def test_l2l3_gate_off_byte_identical(monkeypatch, tmp_path):
    """Both flags OFF → no fork, no approve block, no hard-check (pre-#1344 engine)."""
    _setup(monkeypatch, tmp_path, gate=False, detect=False)
    assert gx10.CONSTRAINT_GATE_ENABLED is False
    assert gx10.CONSTRAINT_CONFLICT_DETECT is False

    # Direct capture (tool is gate-gated); typed floor exists but detect/hardcheck are off.
    gx10.record_constraints("C", "python only", language="python")
    out = gx10.run_tool(
        "record_design",
        {"title": "D", "body": "use rust", "language": "rust"},
    )
    assert out.startswith("OK: design proposal recorded at ")
    assert not _forks_dir().exists()
    assert gx10._load_fork_envelopes(_slug()) == []

    # /fork falls through to the M5 unit-proposal empty path (exact pre-S4 string).
    fork_out = gx10._fork_command("")
    assert fork_out == _PRE_S4_FORK_EMPTY
    assert "fork_id:" not in fork_out
    assert "Typed HARD conflicts" not in fork_out

    # /approve design is NOT blocked by pending forks / hard-check.
    approve = gx10._approve_design()
    assert "pending constraint fork" not in approve.lower()
    assert approve.startswith("OK") or "already approved" in approve.lower()
    fm = gx10._parse_frontmatter(_design_doc().read_text(encoding="utf-8"))
    assert fm.get("approved") == "true"

    # Conflicting impl task language is NOT refused (no L3 hard-check).
    stage = gx10.run_tool(
        "stage_handover",
        {
            "agent": "OPUS",
            "handover_md": "handover body",
            "task_json": _impl_json("free", language="rust"),
        },
    )
    assert stage.startswith("OK")
    assert "HARD constraint" not in stage
    assert "refused" not in stage.lower()
    assert len(_pending()) == 1
    tid = _pending()[0]["id"]
    md = _handover_text(tid)
    # Gate off ⇒ no IRONCLAD:CONSTRAINTS injection either (L1 off).
    assert "IRONCLAD:CONSTRAINTS" not in md
