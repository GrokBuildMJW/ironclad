"""Composed-gate EXECUTION e2e for the machine-gated dev-loop (epic #312 S7 / ADR-0002), offline.

`test_devloop_e2e` pins the no-op gate (``target=None``) + the coupling halts. This pins the OTHER half
the round-1 review flagged: when a target descriptor is given, ``build_real_ops`` **executes** a composed
gate from the integrity-pinned base — NOT a ``sys.exit(0)`` stub. A LIGHT fixture target (one boundary
guard pointing at a fixture script) proves the ORCHESTRATION — ``compose`` + ``create_pinned_base`` +
``english_only`` + dispose + the driver state machine + blocked-routing — without the heavy real
export/pytest scripts:

  (1) a clean unit              -> the composed gate runs the fixture guard, GREEN -> MERGE human-stop;
  (2) a unit that trips it      -> the composed gate goes RED -> halt at GATE (the round-1 S1 proof that
                                   the gate is EXECUTED, not a no-op);
  (3) a self-modifying unit     -> BLOCKED-for-review, before the gate is ever reached.

The LIVE proof — a real ``claude --print`` on a real mjw_agentic issue driving real CI green to the
MERGE-stop — is the operator's C2 confirmation (documented in ``vault/Plan/phase2-c2-live-activation.md``,
not a CI unit test).
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_E2E = _REPO / "scripts" / "devloop" / "e2e.py"

pytestmark = pytest.mark.skipif(
    not _E2E.is_file(),
    reason="private dev-loop e2e (scripts/devloop/e2e.py) absent — clean-room tree",
)


def _load():
    spec = importlib.util.spec_from_file_location("_devloop_e2e_core", _E2E)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


# The fixture's stand-in for a real shell gate guard: exits 1 iff a sentinel 'BOOM' file is present (a
# boundary-violation analog), printing a recognisable marker — so the SAME target proves both a green and
# a red composed gate, isolating the RED cause to the executed guard (not coupling/english-only).
_GATE_CHECK = (
    "import os, sys\n"
    "if os.path.exists('BOOM'):\n"
    "    sys.stderr.write('GATE_CHECK: BOOM sentinel present -- boundary violation\\n')\n"
    "    sys.exit(1)\n"
    "sys.exit(0)\n"
)

# A LIGHT target: one boundary guard pointing at the fixture script. gate_plan emits NO stage step (no
# doc-reality-audit/secret-scan in the profile), so no heavy export/pytest runs — only compose + the
# pinned-base + english-only, which IS the orchestration under test.
_FIXTURE_TARGET = {"boundary_cmd": "python gate_check.py", "gate_profile": ["boundary"]}


def _init_fixture(root: Path) -> None:
    root.mkdir(parents=True)
    subprocess.run(["git", "-c", "init.defaultBranch=main", "init", str(root)], check=True, capture_output=True)
    _git(root, "config", "user.name", "t")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "core.autocrlf", "false")   # deterministic LF so the pinned-base diff applies cleanly
    (root / "gate_check.py").write_text(_GATE_CHECK, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "seed")


def _agent_factory(*, boom: bool = False, self_mod: bool = False):
    """A fake coder-agent that writes into the worktree. boom => trips the fixture gate guard;
    self_mod => touches the protected class (scripts/devloop/)."""
    def agent(handle, argv):
        wt = Path(handle.path)
        if self_mod:
            (wt / "scripts" / "devloop").mkdir(parents=True, exist_ok=True)
            (wt / "scripts" / "devloop" / "x.py").write_text("VALUE = 1\n", encoding="utf-8")
        else:
            (wt / "src.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        (wt / "tests").mkdir(exist_ok=True)
        (wt / "tests" / "test_x.py").write_text("def test_f():\n    assert True\n", encoding="utf-8")
        if boom:
            (wt / "BOOM").write_text("violation\n", encoding="utf-8")
        return (0, "ok")
    return agent


def _run(e2e, tmp_path: Path, agent, issue: int = 328):
    repo = tmp_path / "repo"
    _init_fixture(repo)
    ops = e2e.build_real_ops(repo, tmp_path / "wt", agent,
                             target=_FIXTURE_TARGET, base_path=tmp_path / "base")
    unit = e2e.Unit(issue=issue, branch=f"feat/devloop-e2e-{issue}")
    return e2e.Driver(ops).run(unit, ["agent"])


def test_composed_gate_runs_and_green_unit_reaches_merge_stop(tmp_path):
    # the REAL composed gate (not the sys.exit(0) stub) runs the fixture guard from the pinned base,
    # is GREEN, and the dial-frozen driver stops at the MERGE human gate (never auto-merges).
    e2e = _load()
    out = _run(e2e, tmp_path, _agent_factory())
    assert out.state == "MERGE" and out.status == "stopped-at-human-gate"
    assert out.worktree_disposed
    gate = [r for r in out.trace if r["src"] == "GATE" and r["dst"] == "GATE" and r["guard"] == "gate"]
    assert gate and gate[0]["passed"]                                   # the COMPOSED gate ran + passed
    # Produce != Apply (ADR-0002 D3): the gate-validated worktree was APPLIED onto the unit branch — a
    # reviewable commit remains for the human at the frozen MERGE stop (the driver still never merges/pushes).
    assert any(r["guard"] == "apply" and r["passed"] for r in out.trace)
    repo = tmp_path / "repo"
    branch_head = subprocess.run(["git", "-C", str(repo), "rev-parse", "feat/devloop-e2e-328"],
                                 capture_output=True, text=True).stdout.strip()
    main_head = subprocess.run(["git", "-C", str(repo), "rev-parse", "main"],
                               capture_output=True, text=True).stdout.strip()
    assert branch_head and branch_head != main_head                    # the branch advanced past main
    shown = subprocess.run(["git", "-C", str(repo), "show", "--stat", "feat/devloop-e2e-328"],
                           capture_output=True, text=True).stdout
    assert "src.py" in shown                                            # the agent's produced file is in the commit


def test_composed_gate_red_unit_halts_at_gate(tmp_path):
    # the round-1 S1 proof: the composed gate is EXECUTED — a unit that trips the executed guard goes RED
    # and halts at GATE (a no-op sys.exit(0) gate would have wrongly reached MERGE). The unit passes
    # coupling (code+test present) so the RED is the gate's, isolated to OUR fixture guard.
    e2e = _load()
    out = _run(e2e, tmp_path, _agent_factory(boom=True))
    assert out.status == "halted" and out.state == "GATE"
    assert any("GATE_CHECK" in r or "BOOM" in r for r in out.reasons)    # our executed fixture guard failed it
    assert out.worktree_disposed


def test_self_modifying_unit_is_blocked_before_the_gate(tmp_path):
    # a diff touching the protected class (scripts/devloop/) is propose-only: routed to a terminal
    # BLOCKED-for-review state BEFORE the gate — a green-but-self-modifying gate can never auto-merge.
    e2e = _load()
    out = _run(e2e, tmp_path, _agent_factory(self_mod=True))
    assert out.state == "BLOCKED" and out.status == "blocked-for-review"
    assert any("protected" in r for r in out.reasons)
    assert not any(r["src"] == "GATE" for r in out.trace)               # never reached the gate
    assert out.worktree_disposed


# ── #348 S4 DELIVER composer (deliver_plan, parallel to gate_plan) ──
def test_deliver_plan_orders_stage_first_then_delivery_gates(tmp_path):
    # the DELIVER composer stages the export FIRST (the delivery gates read it), then the delivery gates in
    # the PRE-publish order: verify the staged tree -> assert the version -> build the wheel from it.
    e2e = _load()
    target = {"dod_profile": ["clean-room", "release-preflight", "export-sync"]}
    plan = e2e.deliver_plan(target, tmp_path, tag="v1.2.3")
    assert [n for n, _ in plan] == ["stage-export", "export-sync", "release-preflight", "clean-room"]
    assert plan[0][1][-1] == "--require-scanner" and plan[0][1][1].endswith("export_core.py")   # stage + scan
    assert "v1.2.3" in dict(plan)["release-preflight"]                                           # tag threaded


def test_deliver_surface_compose_fails_on_a_red_gate(tmp_path):
    # symmetric to the GATE red proof (test_composed_gate_red_unit_halts_at_gate): a red delivery gate makes
    # the composed DELIVER fail, never a silent pass. Here the delivery-gate scripts are absent under the
    # tmp base, so each shell_guard fails closed (could-not-run) -> the composed DELIVER is RED.
    e2e = _load()
    plan = e2e.deliver_plan({"dod_profile": ["clean-room", "export-sync"]}, tmp_path, tag="v1")
    results = [(n, e2e.guards.shell_guard(n, argv, tmp_path)) for n, argv in plan if n in ("export-sync", "clean-room")]
    composed = e2e.guards.compose("deliver", [r for _, r in results])
    assert not composed.passed and composed.reasons                # a red delivery gate fails the composed DELIVER
    assert all(not r.passed and r.name == n for n, r in results)   # both delivery gates are RED, named correctly


def test_build_real_deliver_ops_returns_a_wired_deliverops(tmp_path):
    # #348 S8: the assembly wires the 5 DELIVER seams (no execution here — the seams are closures).
    e2e = _load()
    target = {"default_base_branch": "main", "dod_profile": ["clean-room"], "repo": "o/r"}
    ops = e2e.build_real_deliver_ops(
        str(tmp_path), target, str(tmp_path / "base"), unit=357, tag="v1", go="", operator="op",
        secret=b"s", tree_sha="t", version="1", ledger_path=str(tmp_path / "l"),
        release_repo="o/r", runner=lambda n, a: (0, ""))
    assert all(callable(getattr(ops, s)) for s in ("stage_base", "deliver_gate", "authorize", "execute", "dispose"))


def _deliver_ops_for(tmp_path, **over):
    e2e = _load()
    target = {"default_base_branch": "main", "dod_profile": ["clean-room"], "repo": "o/mono"}
    kw = dict(unit=364, tag="v1", go="", operator="op", secret=b"s", tree_sha="treeabc", version="1",
              ledger_path=str(tmp_path / "ledger.jsonl"), release_repo="o/ironclad")
    kw.update(over)
    ran = {"calls": []}
    ops = e2e.build_real_deliver_ops(str(tmp_path), target, str(tmp_path / "base"),
                                     runner=lambda n, a: (ran["calls"].append(n) or (0, "ok")), **kw)
    return e2e, ops, ran, kw["ledger_path"]


def _ledger_payloads(path):
    import json
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line).get("payload", {}))
    return out


def test_deliver_leg_refuses_an_over_scoped_declared_token(tmp_path, monkeypatch):
    # #396 S14b (CC-1): the DELIVER leg gets the per-unit leg's declared-token-scope refusal — a token
    # DECLARED to reach beyond {unit_target, release_repo} fails CLOSED before the push (runner NOT called).
    _e2e, ops, ran, _lp = _deliver_ops_for(tmp_path)
    monkeypatch.setenv("GX10_DEVLOOP_TOKEN_TARGETS", "o/mono,o/ironclad,o/pypi")   # pypi is outside the allowed set
    delivered, log = ops.execute(True, "GO ok")
    assert not delivered and any("o/pypi" in str(x) for x in log) and ran["calls"] == []   # refused, nothing pushed


def test_deliver_leg_scope_passes_when_declared_matches(tmp_path, monkeypatch):
    _e2e, ops, ran, _lp = _deliver_ops_for(tmp_path)
    monkeypatch.setenv("GX10_DEVLOOP_TOKEN_TARGETS", "o/mono,o/ironclad")           # exactly the allowed set
    delivered, _log = ops.execute(True, "GO ok")
    assert delivered and ran["calls"] == ["mirror-push", "release-create"]          # scope ok -> the push runs


def test_deliver_leg_unauthorized_never_admits_the_delivery_target(tmp_path, monkeypatch):
    # deliver_scope is bound to the authorize result: unauthorized -> the delivery target is NOT in the
    # allowed set, so a declared token reaching it is refused even on the DELIVER leg.
    _e2e, ops, ran, _lp = _deliver_ops_for(tmp_path)
    monkeypatch.setenv("GX10_DEVLOOP_TOKEN_TARGETS", "o/mono,o/ironclad")
    delivered, log = ops.execute(False, "no GO")                                     # authorized=False
    assert not delivered and any("o/ironclad" in str(x) for x in log) and ran["calls"] == []


def test_stamp_writes_a_delivered_pending_record(tmp_path, monkeypatch):
    # #396 S14b: the push-time stamp is delivered-pending (NOT terminal); inert marker without K.
    monkeypatch.delenv("GX10_DEVLOOP_MARKER_KEY", raising=False)
    _e2e, ops, _ran, lp = _deliver_ops_for(tmp_path)
    ops.stamp()
    pays = _ledger_payloads(lp)
    assert pays and pays[-1]["surface"] == "DELIVER" and pays[-1]["status"] == "delivered-pending"
    assert pays[-1]["unit"] == 364 and pays[-1]["tree_sha"] == "treeabc" and pays[-1]["marker"] is None


def test_log_seam_persists_outcomes_with_delivery_identity(tmp_path):
    # #396 S14b (D2-6): the log seam persists every DeliverOutcome enriched with sha/tree_sha/unit so a
    # half-shipped (delivered-unrecorded) release is visible to the reconciler (it keys on sha).
    _e2e, ops, _ran, lp = _deliver_ops_for(tmp_path)
    ops.log({"surface": "DELIVER", "state": "DELIVER", "status": "delivered-unrecorded", "reasons": ["x"]})
    pays = _ledger_payloads(lp)
    assert pays[-1]["status"] == "delivered-unrecorded" and pays[-1]["sha"] == "treeabc" and pays[-1]["unit"] == 364
