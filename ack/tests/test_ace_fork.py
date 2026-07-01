"""ACE-FORKSIG (#855 / #882, M5-1): the pure fork-signal data contract (MPR-A-1). ForkSignal/ForkResolution
lossless round-trip + drift-tolerant from_dict + the pure ledger adapter (FORK / FORK_RESOLVED surfaces),
never-raising on garbage. Pure/stdlib-only — no engine/scripts import.
"""
from __future__ import annotations

from ack.ace import (ForkSignal, ForkResolution, parse_fork_signal, parse_fork_resolution,
                     fork_signals_from, fork_resolutions_from, FORK_SURFACE, FORK_RESOLVED_SURFACE)


# ─── round-trip ──────────────────────────────────────────────────────────────────────────────────────
def test_fork_signal_round_trip_lossless():
    s = ForkSignal(unit="880", area="engine", question="A or B seam?",
                   options=["ledger-derived", "event-seam"], touched_paths=["engine/gx10.py"])
    d = s.to_dict()
    assert d["surface"] == FORK_SURFACE
    assert ForkSignal.from_dict(d) == s


def test_fork_resolution_round_trip_lossless():
    r = ForkResolution(unit="880", area="engine", chosen_option="ledger-derived", outcome="delivered")
    d = r.to_dict()
    assert d["surface"] == FORK_RESOLVED_SURFACE
    assert ForkResolution.from_dict(d) == r


# ─── drift tolerance ─────────────────────────────────────────────────────────────────────────────────
def test_from_dict_is_drift_tolerant_and_never_raises():
    thin = ForkSignal.from_dict({"unit": 880, "question": "just this"})   # missing area/options/paths, int unit
    assert thin.unit == "880" and thin.question == "just this" and thin.options == []
    assert ForkSignal.from_dict({"options": "not-a-list", "extra": "ignored"}).options == []
    assert ForkSignal.from_dict(None).is_empty() and ForkSignal.from_dict("garbage").is_empty()
    assert ForkResolution.from_dict({}).is_empty()


def test_options_zero_one_many():
    assert ForkSignal.from_dict({"unit": "1", "question": "q", "options": []}).options == []
    assert ForkSignal.from_dict({"unit": "1", "question": "q", "options": ["only"]}).options == ["only"]
    many = ForkSignal.from_dict({"unit": "1", "question": "q", "options": ["a", "", "b", None, "c"]})
    assert many.options == ["a", "b", "c"]                 # blanks/None dropped


# ─── the pure ledger adapter ─────────────────────────────────────────────────────────────────────────
def test_parse_fork_signal_only_on_fork_surface():
    good = {"surface": FORK_SURFACE, "unit": "880", "question": "A or B?", "options": ["A", "B"]}
    assert parse_fork_signal(good).unit == "880"
    assert parse_fork_signal({"surface": "DELIVER", "status": "delivered"}) is None   # not a fork record
    assert parse_fork_signal({"surface": FORK_SURFACE}) is None                       # empty fork → None
    # a full ledger record wrapper is unwrapped
    assert parse_fork_signal({"seq": 3, "payload": good}).unit == "880"


def test_parse_fork_resolution_only_on_resolved_surface():
    good = {"surface": FORK_RESOLVED_SURFACE, "unit": "880", "chosen_option": "A", "outcome": "delivered"}
    assert parse_fork_resolution(good).chosen_option == "A"
    assert parse_fork_resolution({"surface": FORK_SURFACE, "unit": "880", "question": "q"}) is None


def test_fork_signals_and_resolutions_from_mixed_ledger():
    ledger = [
        {"surface": FORK_SURFACE, "unit": "701", "question": "seam?", "options": ["A", "B"]},
        {"unit": 701, "src": "IMPLEMENT", "dst": "GATE", "guard": "gate", "passed": True},   # a driver leg (ignored)
        {"surface": FORK_RESOLVED_SURFACE, "unit": "701", "chosen_option": "A", "outcome": "delivered"},
        {"surface": FORK_SURFACE, "unit": "702", "question": "other?", "options": ["X"]},
        None, "garbage", 123, {},                                                            # never raises
    ]
    sigs = fork_signals_from(ledger)
    res = fork_resolutions_from(ledger)
    assert [s.unit for s in sigs] == ["701", "702"]        # both FORK signals, in order; driver leg + garbage skipped
    assert len(res) == 1 and res[0].unit == "701" and res[0].chosen_option == "A"


def test_from_empty_and_none():
    assert fork_signals_from([]) == [] and fork_signals_from(None) == []
    assert fork_resolutions_from([]) == []
