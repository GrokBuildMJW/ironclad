"""The dev-loop driver / state machine (epic #262, S4 / ADR 0002), offline.

Lives in `scripts/devloop/` (private) -> skips in an installed/clean-room tree. Drives the
orchestration with **injected fake I/O** (no real git/gh/agent): the happy path stops at the MERGE
human gate with the worktree disposed; a red guard at any stage **halts fail-closed** and still
disposes the worktree; a bad branch halts before a worktree is even created; the driver never
merges/markers on its own (dial frozen).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_DRIVER = _REPO / "scripts" / "devloop" / "driver.py"

pytestmark = pytest.mark.skipif(
    not _DRIVER.is_file(),
    reason="private dev-loop driver (scripts/devloop/driver.py) absent — clean-room tree",
)


def _load():
    spec = importlib.util.spec_from_file_location("_devloop_driver", _DRIVER)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _ops(d, *, rc=0, changed=None, viol=None, gate_ok=True, pr="Closes #266"):
    """A DriverOps wired with fakes; `state` records what happened."""
    from guards import GuardResult  # the sibling module driver.py put on sys.path
    state = {"created": 0, "disposed": 0, "log": []}
    # default = an ORDINARY (non-protected) unit so the happy path reaches MERGE; a self-modifying
    # diff (scripts/devloop/**) is routed to BLOCKED — see test_self_mod_protected_unit_is_blocked.
    changed = changed if changed is not None else ["ack/x.py", "ack/tests/test_devloop_x.py"]

    def create(unit):
        state["created"] += 1
        return {"branch": unit.branch}

    ops = d.DriverOps(
        create_worktree=create,
        run_agent=lambda h, argv: (rc, "ok" if rc == 0 else "boom"),
        changed_files=lambda h: changed,
        confinement=lambda unit: (viol or []),
        gate_runner=lambda unit, h: GuardResult("gate", gate_ok, [] if gate_ok else ["pytest red"]),
        pr_body=lambda unit: pr,
        ci_check=lambda unit: GuardResult("ci", True),
        dispose=lambda h: state.__setitem__("disposed", state["disposed"] + 1),
        log=lambda rec: state["log"].append(rec),
    )
    return ops, state


def _unit(d, branch="feat/devloop-driver-266", labels=()):
    return d.Unit(issue=266, branch=branch, labels=list(labels))


def test_happy_path_stops_at_merge_and_disposes():
    d = _load()
    ops, state = _ops(d)
    out = d.Driver(ops).run(_unit(d), ["agent"])
    assert out.state == "MERGE" and out.status == "stopped-at-human-gate"
    assert out.worktree_disposed and state["disposed"] >= 1     # finally always disposes
    assert any(r["dst"] == "MERGE" for r in out.trace)          # reached the human gate
    assert state["created"] == 1


def test_apply_persists_a_green_unit_and_a_failed_apply_halts_fail_closed():
    # Produce != Apply (ADR-0002 D3): ONLY a green unit is applied (committed onto the branch), exactly
    # once, and the run still reaches the frozen MERGE stop.
    d = _load()
    calls = []
    ops, _ = _ops(d)
    ops.apply = lambda unit, h: (calls.append(unit.issue), "abc123def0")[1]
    out = d.Driver(ops).run(_unit(d), ["agent"])
    assert out.state == "MERGE" and calls == [266]
    assert any(r["guard"] == "apply" and r["passed"] for r in out.trace)
    # a failing apply is RED -> halt at GATE, fail-closed (the validated work is never silently dropped).
    ops2, _ = _ops(d)

    def boom(unit, h):
        raise RuntimeError("nothing to commit / fast-forward failed")
    ops2.apply = boom
    out2 = d.Driver(ops2).run(_unit(d), ["agent"])
    assert out2.status == "halted" and out2.state == "GATE" and out2.guard == "apply"
    assert any("apply failed" in x for x in out2.reasons)


def test_self_mod_protected_unit_is_blocked_for_review():
    # ADR-0002 D5 / #312 S2: a diff touching the engine is propose-only -> terminal BLOCKED, NOT GATE/MERGE,
    # even though the (faked) gate is green. Independent of dial position.
    d = _load()
    ops, state = _ops(d, changed=["scripts/devloop/driver.py", "ack/tests/test_devloop_x.py"], gate_ok=True)
    out = d.Driver(ops).run(_unit(d), ["agent"])
    assert out.state == "BLOCKED" and out.status == "blocked-for-review"
    assert out.guard == "self-mod-protected" and "out-of-band" in out.reasons[0]
    assert not any(r["dst"] in ("GATE", "MERGE") for r in out.trace)   # never reached the gate/merge
    assert out.worktree_disposed and state["disposed"] >= 1            # still torn down


def test_bad_branch_halts_before_a_worktree_exists():
    d = _load()
    ops, state = _ops(d)
    out = d.Driver(ops).run(_unit(d, branch="not-a-valid-branch"), ["agent"])
    assert out.status == "halted" and out.guard == "branch-format"
    assert state["created"] == 0 and state["disposed"] == 0     # no worktree was ever created


def test_red_gate_halts_fail_closed_and_disposes():
    d = _load()
    ops, state = _ops(d, gate_ok=False)
    out = d.Driver(ops).run(_unit(d), ["agent"])
    assert out.status == "halted" and out.state == "GATE" and "pytest red" in out.reasons
    assert state["disposed"] >= 1                               # worktree disposed even on halt


def test_confinement_violation_halts_and_disposes():
    d = _load()
    ops, state = _ops(d, viol=["leaked.txt"])
    out = d.Driver(ops).run(_unit(d), ["agent"])
    assert out.status == "halted" and out.guard == "confinement"
    assert state["disposed"] >= 1


def test_agent_error_halts_at_implement():
    d = _load()
    ops, state = _ops(d, rc=1)
    out = d.Driver(ops).run(_unit(d), ["agent"])
    assert out.status == "halted" and out.state == "IMPLEMENT"
    assert state["disposed"] >= 1
