"""#1343 (S7) — L1 capstone: real-tool-dispatch E2E + gate-off byte-identical.

Integration proof for the full L1 flow through the REAL ``run_tool`` /
``_run_tool_dispatch`` path (not direct function calls for the gated verbs),
asserting orchestrator call ORDER, plus the comprehensive gate-off byte-identical
case (#29 boundary-clean — public core contract only, no private-driver import).

Test-only capstone — no engine changes. Reuses the ``test_design_gate`` /
``test_constraints`` harness.
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


def _setup(monkeypatch, tmp_path, *, gate=False):
    gx10._apply_config(gx10._code_defaults())
    monkeypatch.setattr(gx10, "DESIGN_GATE_ENABLED", False)  # isolate the constraint gate
    monkeypatch.setattr(gx10, "CONSTRAINT_GATE_ENABLED", gate)
    gx10.STORE = None
    monkeypatch.setattr(gx10, "_ui_print", lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)
    gx10.initiative_new("Demo", "software")


def _impl_json(title: str = "build it") -> str:
    return json.dumps({"type": "implementation", "priority": "high", "title": title, "description": "x"})


def _pending():
    return gx10._store().list("pending")


def _constraints_doc() -> Path:
    return gx10.vault_root() / gx10.active_slug() / "decisions" / "constraints.md"


def _design_doc() -> Path:
    return gx10.vault_root() / gx10.active_slug() / "decisions" / "design.md"


def _tool_names() -> set[str]:
    return {tool["function"]["name"] for tool in gx10._effective_tools()}


def _handover_path(tid: str) -> Path:
    matches = list(gx10.handovers_dir().glob(f"{tid}_*.md"))
    assert len(matches) == 1, f"expected one handover for {tid}, found {matches}"
    return matches[0]


def _handover_text(tid: str) -> str:
    return _handover_path(tid).read_text(encoding="utf-8")


def _block_count(md: str) -> int:
    return len(re.findall(r"<!-- IRONCLAD:CONSTRAINTS -->", md))


def test_l1_full_flow_through_real_tool_dispatch(monkeypatch, tmp_path):
    """Full L1 order via real ``run_tool``: constraints → (gate) → design → approve → block.

    web_search is not scripted; this stands in for the post-research state — the capture
    intent is what the orchestrator must enforce after research lands.
    """
    _setup(monkeypatch, tmp_path, gate=True)
    constraint_body = "Language: Python only. No network."

    # BEFORE constraints: implementation stage_handover is refused; nothing created.
    refused_ho = gx10.run_tool(
        "stage_handover",
        {
            "agent": "OPUS",
            "handover_md": "coder body",
            "task_json": _impl_json("pre-gate impl"),
        },
    )
    assert "constraints not on record" in refused_ho
    assert "record_constraints" in refused_ho
    assert _pending() == []
    assert not list(gx10.handovers_dir().glob("*.md"))

    # BEFORE constraints: record_design is refused; nothing created.
    refused_design = gx10.run_tool(
        "record_design",
        {"title": "Approach", "body": "use Rust"},
    )
    assert refused_design.startswith("ERROR:") and "constraints not on record" in refused_design
    assert not _design_doc().exists()

    # 1) record_constraints through real dispatch → CAPTURED.
    cap = gx10.run_tool(
        "record_constraints",
        {"title": "Scope floor", "body": constraint_body, "language": "python", "network": "none"},
    )
    assert cap.startswith("OK: constraints recorded at ") and cap.endswith("(CAPTURED).")
    assert _constraints_doc().is_file()
    assert gx10._constraint_status(gx10.active_slug()) == (gx10.CAPTURED, constraint_body)

    # 3) After capture: record_design passes the constraint gate.
    design_out = gx10.run_tool(
        "record_design",
        {"title": "Approach", "body": "use Rust"},
    )
    assert design_out.startswith("OK: design proposal recorded at ")
    assert "approved: false" in design_out
    assert _design_doc().is_file()
    fm = gx10._parse_frontmatter(_design_doc().read_text(encoding="utf-8"))
    assert fm.get("type") == "proposal" and fm.get("approved") == "false"

    # 4) /approve path (or direct helper) promotes the design.
    approve_msg = gx10._approve_design()
    assert approve_msg.startswith("OK")
    fm = gx10._parse_frontmatter(_design_doc().read_text(encoding="utf-8"))
    assert fm.get("type") == "decision" and fm.get("approved") == "true"

    # 5) IMPLEMENTATION handover via real tool dispatch carries constraints VERBATIM
    # inside exactly ONE IRONCLAD:CONSTRAINTS block; coder body follows after ``\n\n``.
    coder_body = "coder body: implement the design"
    stage_out = gx10.run_tool(
        "stage_handover",
        {
            "agent": "OPUS",
            "handover_md": coder_body,
            "task_json": _impl_json("build it"),
        },
    )
    assert stage_out.startswith("OK")
    assert "refused" not in stage_out.lower()
    assert "constraints not on record" not in stage_out
    assert len(_pending()) == 1
    tid = _pending()[0]["id"]
    md = _handover_text(tid)

    open_m, close_m = gx10._CONSTRAINT_MARKERS
    assert _block_count(md) == 1
    assert open_m in md and close_m in md
    assert "## Constraints (authoritative — honour verbatim; do not override)" in md
    inner = md.split(open_m, 1)[1].split(close_m, 1)[0]
    assert constraint_body in inner  # verbatim body inside the single block
    after = md.split(close_m, 1)[1]
    assert after.startswith("\n\n")
    assert coder_body in after


def test_gate_off_byte_identical(monkeypatch, tmp_path):
    """Gate OFF → no refusal, no block, tool not offered, dispatch disabled; staged text
    equals the pre-feature enrichment output (constraint_snapshot=None)."""
    _setup(monkeypatch, tmp_path, gate=False)
    assert gx10.CONSTRAINT_GATE_ENABLED is False
    assert "record_constraints" not in _tool_names()
    assert gx10.run_tool(
        "record_constraints",
        {"title": "Scope", "body": "stay local"},
    ) == "ERROR: constraint gate disabled"
    assert not _constraints_doc().exists()

    body = "handover body"
    # Real staging path with gate OFF and no constraints.md (default / pre-feature).
    out = gx10._stage_handover(None, "OPUS", body, _impl_json())
    assert out.startswith("OK")
    assert "refused" not in out.lower()
    assert "constraints not on record" not in out
    assert len(_pending()) == 1
    tid = _pending()[0]["id"]
    md = (gx10.handovers_dir() / f"{tid}_OPUS.md").read_text(encoding="utf-8")
    assert "IRONCLAD:CONSTRAINTS" not in md

    # Pre-feature output: enrich with no constraint snapshot (byte-identical off path).
    task = gx10._store().get(tid)
    expected = gx10._enrich_handover(
        tid, body, task, [], "OPUS", constraint_snapshot=None,
    )
    assert md == expected


def test_config_path_activation(monkeypatch, tmp_path):
    """Synthetic ``_apply_config`` enables the gate (#1345 wiring) — no private conf read."""
    _setup(monkeypatch, tmp_path, gate=False)
    assert gx10.CONSTRAINT_GATE_ENABLED is False
    assert "record_constraints" not in _tool_names()

    # Synthetic dict only — never conf/local.json from a core test.
    cfg = gx10._code_defaults()
    cfg.update({"constraint_gate": {"enabled": True}})
    gx10._apply_config(cfg)

    assert gx10.CONSTRAINT_GATE_ENABLED is True
    assert "record_constraints" in _tool_names()
    # Gate is active: an implementation handover is refused until constraints are captured.
    refused = gx10.run_tool(
        "stage_handover",
        {
            "agent": "OPUS",
            "handover_md": "coder body",
            "task_json": _impl_json("config-path impl"),
        },
    )
    assert "constraints not on record" in refused
    assert _pending() == []


def test_captured_none_e2e(monkeypatch, tmp_path):
    """CAPTURED_NONE via real dispatch: gate passes, injects nothing, strips a stale block."""
    _setup(monkeypatch, tmp_path, gate=True)

    out = gx10.run_tool(
        "record_constraints",
        {"title": "No constraints", "body": "none"},
    )
    assert out.startswith("OK: constraints recorded at ") and out.endswith("(CAPTURED_NONE).")
    assert gx10._constraint_status(gx10.active_slug()) == (gx10.CAPTURED_NONE, None)

    stale = (
        "<!-- IRONCLAD:CONSTRAINTS -->\n"
        "## Constraints\nstale body\n"
        "<!-- /IRONCLAD:CONSTRAINTS -->\n\n"
        "coder body after none"
    )
    stage = gx10.run_tool(
        "stage_handover",
        {
            "agent": "OPUS",
            "handover_md": stale,
            "task_json": _impl_json("none-impl"),
        },
    )
    assert stage.startswith("OK")
    assert "refused" not in stage.lower()
    assert "constraints not on record" not in stage
    assert len(_pending()) == 1
    md = _handover_text(_pending()[0]["id"])
    assert "IRONCLAD:CONSTRAINTS" not in md
    assert "stale body" not in md
    assert "coder body after none" in md
