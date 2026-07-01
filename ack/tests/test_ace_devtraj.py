"""ACE-DEVTRAJ (#855 / #878, M4-1): the pure dev-process ledger → Trajectory adapter (DP-1). Fixture-driven:
green-merge / blocked-at-GATE / blocked-at-REVIEW / inert-review / aborted / multi-unit interleave /
DELIVER-without-unit / garbage — schema-drift tolerant, never raises, label-free, used_bullet_ids=[].
"""
from __future__ import annotations

from ack.ace import ledger_to_trajectories, Trajectory


def _leg(unit, src, dst, guard, passed, reasons=None):
    return {"unit": unit, "src": src, "dst": dst, "guard": guard, "passed": passed, "reasons": reasons or []}


def _by_query(trajs):
    return {t.query: t for t in trajs}


def test_green_merge_unit_reaches_human_merge_gate():
    payloads = [_leg(42, "IMPLEMENT", "GATE", "gate", True),
                _leg(42, "GATE", "REVIEW", "review-evidence", True),
                _leg(42, "REVIEW", "MERGE", "merge-go", True)]
    trajs = ledger_to_trajectories(payloads)
    assert len(trajs) == 1
    t = trajs[0]
    assert t.query == "42" and t.outcome == "reached-human-merge-gate" and t.used_bullet_ids == []
    assert "GATE gate passed" in t.steps and "REVIEW review-evidence passed" in t.steps


def test_blocked_at_gate_carries_failed_step_and_reason():
    trajs = ledger_to_trajectories([_leg(43, "IMPLEMENT", "GATE", "coupling", False,
                                         ["core/ imports a private module"])])
    assert len(trajs) == 1 and trajs[0].outcome == "blocked"
    assert any("FAILED" in s and "coupling" in s and "private module" in s for s in trajs[0].steps)


def test_blocked_at_review():
    trajs = ledger_to_trajectories([_leg(44, "GATE", "GATE", "gate", True),
                                    _leg(44, "GATE", "REVIEW", "review-evidence", False, ["S2 finding unresolved"])])
    assert trajs[0].outcome == "blocked" and any("FAILED" in s for s in trajs[0].steps)


def test_inert_review_is_marked_and_not_a_pass():
    trajs = ledger_to_trajectories([_leg(45, "GATE", "REVIEW", "review-evidence", True,
                                         ["dry-run: review-evidence not enforced (inert)"])])
    t = trajs[0]
    assert "REVIEW review-evidence (inert)" in t.steps
    assert t.outcome == "in-progress"          # an inert review is no merge + no fail → not terminal


def test_aborted_unit():
    trajs = ledger_to_trajectories([_leg(46, "IMPLEMENT", "GATE", "gate", True),
                                    {"abort": 46, "reason": "halted by operator", "actions": [], "errors": []}])
    t = trajs[0]
    assert t.query == "46" and t.outcome == "aborted"
    assert any(s.startswith("abort") for s in t.steps)


def test_multi_unit_interleaving_groups_in_first_seen_order():
    payloads = [_leg(50, "IMPLEMENT", "GATE", "gate", True),
                _leg(51, "IMPLEMENT", "GATE", "coupling", False, ["bad"]),
                _leg(50, "REVIEW", "MERGE", "merge-go", True)]
    trajs = ledger_to_trajectories(payloads)
    assert [t.query for t in trajs] == ["50", "51"]            # first-seen unit order
    by = _by_query(trajs)
    assert by["50"].outcome == "reached-human-merge-gate" and by["51"].outcome == "blocked"


def test_deliver_record_without_unit_is_skipped():
    trajs = ledger_to_trajectories([{"surface": "DELIVER", "state": "pushed", "status": "delivered", "reasons": []}])
    assert trajs == []                                          # no unit ⇒ not unit-correlated


def test_full_ledger_record_wrapper_is_unwrapped():
    rec = {"seq": 7, "prev_hash": "x", "hash": "y", "payload": _leg(60, "IMPLEMENT", "GATE", "gate", True)}
    trajs = ledger_to_trajectories([rec])
    assert len(trajs) == 1 and trajs[0].query == "60"


def test_garbage_and_partial_never_raises():
    assert ledger_to_trajectories([]) == []
    assert ledger_to_trajectories(None) == []
    # non-dicts, a dict with no leg fields, a leg missing keys → no crash, no spurious trajectory
    trajs = ledger_to_trajectories([None, "nope", 123, {}, {"unit": 99}, {"random": "x"}])
    assert trajs == []                                          # {"unit":99} has no dst/src/guard ⇒ not a leg
    # a partial leg (missing guard/reasons) still maps, never raises
    partial = ledger_to_trajectories([{"unit": 1, "dst": "GATE", "passed": True}])
    assert len(partial) == 1 and partial[0].query == "1"
