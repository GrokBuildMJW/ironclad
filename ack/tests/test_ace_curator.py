"""ACE-CURATE (#855 / #859): the Curator — deterministic delta generation + section assignment + the
JSON {reasoning, operations} format + the deterministic merge. Pure / stdlib-only.

Pins F-001 (delta generation), F-002 (section assignment), F-003 (JSON format), C-001 (delta not rewrite),
C-002 (localized updates), N-003 (Curator input = Reflector output).
"""
from __future__ import annotations

import json

import pytest

from ack.ace import (Playbook, Bullet, ReflectorOutput, CandidateBullet, BulletRating,
                     Delta, DeltaOp, curate, apply_delta, OP_ADD, OP_RATE, OP_TAG, OP_REMOVE,
                     HELPFUL, HARMFUL, NEUTRAL)


def _refout():
    return ReflectorOutput(
        insights=[CandidateBullet("write the test first", "strategies_and_hard_rules", [HELPFUL]),
                  CandidateBullet("avoid tool X", "weird-section")],          # bad section → snap (F-002)
        ratings=[BulletRating("b-0", HELPFUL), BulletRating("b-1", HARMFUL)])


def test_curate_is_deterministic_and_maps_insights_and_ratings():
    d1 = curate(_refout())
    d2 = curate(_refout())
    assert d1.to_dict() == d2.to_dict()                                       # deterministic, no LLM
    adds = [o for o in d1.operations if o.op == OP_ADD]
    rates = [o for o in d1.operations if o.op == OP_RATE]
    assert len(adds) == 2 and len(rates) == 2
    assert adds[1].section == "strategies_and_hard_rules"                     # snapped to the first canonical
    assert d1.reasoning == "add 2 insight(s); rate 2 used bullet(s)"


def test_apply_delta_adds_bullets_and_updates_counters_localized():
    pb = Playbook()
    a = pb.add_bullet("seed alpha", "apis_to_use")     # b-0
    b = pb.add_bullet("seed beta", "apis_to_use")      # b-1
    summary = apply_delta(curate(_refout()), pb)
    assert summary["added"] == 2 and summary["rated"] == 2 and summary["skipped"] == 0
    assert a.helpful_count == 1 and HELPFUL in a.tags                          # C-002: only b-0 bumped helpful
    assert b.harmful_count == 1 and HARMFUL in b.tags and b.helpful_count == 0 # only b-1 bumped harmful
    assert len(pb) == 4                                                        # 2 seeds + 2 added insights


def test_rate_and_remove_on_missing_bullet_are_skipped_not_raised():
    pb = Playbook()
    delta = Delta(operations=[DeltaOp(op=OP_RATE, bullet_id="nope", verdict=HELPFUL),
                              DeltaOp(op=OP_REMOVE, bullet_id="also-nope")])
    summary = apply_delta(delta, pb)
    assert summary["skipped"] == 2 and summary["rated"] == 0 and summary["removed"] == 0


def test_add_with_empty_content_is_skipped():
    pb = Playbook()
    summary = apply_delta(Delta(operations=[DeltaOp(op=OP_ADD, content="   ", section="apis_to_use")]), pb)
    assert summary["added"] == 0 and summary["skipped"] == 1 and len(pb) == 0


def test_remove_and_tag_ops_apply():
    pb = Playbook()
    a = pb.add_bullet("x", "apis_to_use")
    s = apply_delta(Delta(operations=[DeltaOp(op=OP_TAG, bullet_id=a.id, tags=["custom"]),
                                      DeltaOp(op=OP_REMOVE, bullet_id=a.id)]), pb)
    assert s["tagged"] == 1 and s["removed"] == 1 and pb.get(a.id) is None


def test_delta_json_round_trip_and_unknown_op_dropped():
    d = curate(_refout())
    rt = Delta.from_json(d.to_json())
    assert rt.to_dict() == d.to_dict()
    # a hand/LLM-emitted delta with a bogus op → that op dropped, valid ops kept (F-003 robust parse)
    raw = json.dumps({"reasoning": "r", "operations": [
        {"op": "frobnicate", "content": "x"},
        {"op": "add", "section": "apis_to_use", "content": "good"}]})
    parsed = Delta.from_json(raw)
    assert len(parsed.operations) == 1 and parsed.operations[0].op == OP_ADD


def test_from_json_garbage_raises():
    with pytest.raises(ValueError):
        Delta.from_json("not json")


def test_neutral_rating_tags_without_counter_change():
    pb = Playbook()
    a = pb.add_bullet("x", "apis_to_use")
    apply_delta(Delta(operations=[DeltaOp(op=OP_RATE, bullet_id=a.id, verdict=NEUTRAL)]), pb)
    assert a.helpful_count == 0 and a.harmful_count == 0 and NEUTRAL in a.tags