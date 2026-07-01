"""ACE-ROBUST (#855 / #866): robustness + safety mechanisms. Pins K-001 (weak-reflector graceful
degradation), K-002 (noisy-update tolerance), K-003 (contradiction detection + resolution), Q-001
(selective item-level unlearning), M-002 (playbook versioning + rollback).
"""
from __future__ import annotations

from ack.ace import (Playbook, adaptation_gain, quarantine_noisy, detect_contradictions,
                     resolve_contradictions, unlearn, version_id, diff_versions, PlaybookHistory)


def _bullet(pb, content, section="strategies_and_hard_rules", helpful=0, harmful=0):
    b = pb.add_bullet(content, section)
    for _ in range(helpful):
        b.mark_helpful()
    for _ in range(harmful):
        b.mark_harmful()
    return b


# ─── K-002: noisy-update tolerance (graceful) ────────────────────────────────────────────────────────
def test_quarantine_removes_net_negative_keeps_good():
    pb = Playbook()
    _bullet(pb, "good rule A", helpful=2)                  # net +2
    _bullet(pb, "neutral rule", helpful=0, harmful=0)      # net 0 — kept (min_net=0)
    _bullet(pb, "noisy rule X", harmful=3)                 # net -3 — quarantined
    rep = quarantine_noisy(pb, min_net=0)
    assert rep["removed"] == 1 and rep["kept"] == 2
    assert "noisy rule X" not in pb.render() and "good rule A" in pb.render()


def test_noise_tolerance_is_graceful_more_corruption_more_pruned():
    pb = Playbook()
    _bullet(pb, "keeper", helpful=1)
    for i in range(5):
        _bullet(pb, f"noise {i}", harmful=2)
    assert quarantine_noisy(pb, min_net=0)["removed"] == 5     # all the net-negative noise dropped
    assert len(pb) == 1 and "keeper" in pb.render()            # the good bullet survives (no collapse)


# ─── K-001: weak-reflector graceful degradation (monotone, both positive) ────────────────────────────
def test_weak_reflector_still_yields_positive_gain_after_quarantine():
    # a WEAK reflector: a couple of good bullets + a lot of noise → raw gain is negative...
    weak = Playbook()
    _bullet(weak, "weak good 1", helpful=1)
    _bullet(weak, "weak good 2", helpful=1)
    for i in range(3):
        _bullet(weak, f"weak noise {i}", harmful=2)
    assert adaptation_gain(weak) < 0                           # raw: noise dominates
    quarantine_noisy(weak, min_net=0)
    assert adaptation_gain(weak) > 0                           # ...but the mechanism makes it a NET GAIN (K-001)
    # a STRONG reflector: more good, no noise → a larger gain (monotone in reflector strength)
    strong = Playbook()
    for i in range(5):
        _bullet(strong, f"strong good {i}", helpful=1)
    quarantine_noisy(strong, min_net=0)
    assert adaptation_gain(strong) > adaptation_gain(weak)     # stronger reflector ⇒ bigger gain


# ─── K-003: contradiction detection + resolution ─────────────────────────────────────────────────────
def test_detect_contradictions_finds_opposite_polarity_same_section():
    pb = Playbook()
    _bullet(pb, "always validate the parser input before running")
    _bullet(pb, "never validate the parser input before running")   # opposite polarity, high overlap
    _bullet(pb, "cache the database handle for reuse")              # unrelated
    conflicts = detect_contradictions(pb)
    assert len(conflicts) == 1
    a, b = conflicts[0]
    assert {pb.get(a).content, pb.get(b).content} == {
        "always validate the parser input before running",
        "never validate the parser input before running"}


def test_no_false_contradiction_for_same_polarity():
    pb = Playbook()
    _bullet(pb, "always validate the parser input")
    _bullet(pb, "always validate the parser output")           # same polarity → not a contradiction
    assert detect_contradictions(pb) == []


def test_modal_verbs_are_not_negations(tmp_path=None):
    # C2 #905: the bare modals "should"/"must" are obligation, not polarity — "you should X" vs "you X"
    # must NOT be a false contradiction.
    pb = Playbook()
    _bullet(pb, "you should write a failing test first")
    _bullet(pb, "you write a failing test first")
    assert detect_contradictions(pb) == []
    # but the genuine negative forms still register a contradiction
    pb2 = Playbook()
    _bullet(pb2, "you should write a failing test first")
    _bullet(pb2, "you shouldnt write a failing test first")     # shouldnt IS a negation
    assert len(detect_contradictions(pb2)) == 1


def test_resolve_contradictions_keeps_the_higher_utility_belief():
    pb = Playbook()
    keep = _bullet(pb, "always validate the parser input", helpful=3)      # reinforced
    drop = _bullet(pb, "never validate the parser input", harmful=1)       # contradicted by experience
    rep = resolve_contradictions(pb)
    assert rep["resolved"] == 1 and drop.id in rep["removed_ids"]
    assert pb.get(keep.id) is not None and pb.get(drop.id) is None


# ─── Q-001: selective item-level unlearning ──────────────────────────────────────────────────────────
def test_unlearn_by_id_single_list_and_missing():
    pb = Playbook()
    a = _bullet(pb, "rule A")
    b = _bullet(pb, "rule B")
    assert unlearn(pb, a.id) == {"removed": 1, "missing": []}              # single id, no retraining
    rep = unlearn(pb, [b.id, "b-999"])
    assert rep["removed"] == 1 and rep["missing"] == ["b-999"]
    assert len(pb) == 0


# ─── M-002: versioning + rollback ────────────────────────────────────────────────────────────────────
def test_version_id_is_deterministic_and_changes_on_edit():
    pb = Playbook()
    _bullet(pb, "rule 1")
    v1 = version_id(pb)
    assert v1 and version_id(pb) == v1                          # stable for an unchanged playbook
    _bullet(pb, "rule 2")
    assert version_id(pb) != v1                                 # any change ⇒ a new identifiable version


def test_diff_versions_tracks_added_and_removed():
    before = Playbook()
    a = _bullet(before, "kept")
    b = _bullet(before, "to remove")
    after = Playbook.from_json(before.to_json())
    after.remove(b.id)
    after.add_bullet("new rule", "apis_to_use")
    d = diff_versions(before, after)
    assert d["removed"] == [b.id] and len(d["added"]) == 1 and d["size_before"] == 2 and d["size_after"] == 2


def test_history_snapshot_and_rollback_restores_a_prior_version():
    pb = Playbook()
    _bullet(pb, "v1 rule")
    hist = PlaybookHistory()
    v1 = hist.snapshot(pb)
    _bullet(pb, "v2 regretted rule")
    v2 = hist.snapshot(pb)
    assert hist.versions() == [v1, v2] and v1 != v2
    restored = hist.rollback()                                  # default: undo the last snapshot → v1
    assert restored is not None and len(restored) == 1 and "v2 regretted rule" not in restored.render()
    assert version_id(restored) == v1
    assert hist.rollback("nonexistent") is None
