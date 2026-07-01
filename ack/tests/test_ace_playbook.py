"""ACE-DATA (#855 / #857): the Bullet + sectioned Playbook data model — pure, stdlib-only.

Pins B-001 (itemized bullet: id + helpful/harmful counters + content), B-002 (sectioned playbook +
explicit render boundaries), B-003 (tagging), M-001 (versioned, lossless round-trip).
"""
from __future__ import annotations

import json

import pytest

from ack.ace import Bullet, Playbook, SCHEMA_VERSION, DEFAULT_SECTIONS, HELPFUL, HARMFUL


def test_add_bullet_mints_unique_stable_ids_into_sections():
    pb = Playbook()
    a = pb.add_bullet("read the file before editing", "strategies_and_hard_rules")
    b = pb.add_bullet("use the X API for Y", "apis_to_use")
    c = pb.add_bullet("always run the test", "strategies_and_hard_rules")
    assert a.id != b.id != c.id and a.id == "b-0" and c.id == "b-2"     # monotonic, unique
    assert [x.id for x in pb.section_bullets("strategies_and_hard_rules")] == ["b-0", "b-2"]  # insertion order
    assert len(pb) == 3 and not pb.is_empty()


def test_empty_content_or_section_is_fail_closed():
    pb = Playbook()
    with pytest.raises(ValueError):
        pb.add_bullet("", "strategies_and_hard_rules")
    with pytest.raises(ValueError):
        pb.add_bullet("x", "   ")


def test_counters_and_net_utility():
    b = Bullet(id="b-0", content="c", section="s")
    b.mark_helpful(); b.mark_helpful(); b.mark_harmful()
    assert b.helpful_count == 2 and b.harmful_count == 1 and b.net_utility == 1


def test_tags_dedup_and_ignore_blank():
    b = Bullet(id="b-0", content="c", section="s")
    b.add_tag(HELPFUL); b.add_tag(HELPFUL); b.add_tag("  "); b.add_tag("custom")
    assert b.tags == [HELPFUL, "custom"]


def test_get_and_remove():
    pb = Playbook()
    a = pb.add_bullet("alpha", "apis_to_use")
    assert pb.get(a.id) is a and pb.get("nope") is None
    assert pb.remove(a.id) is True and pb.get(a.id) is None and pb.remove(a.id) is False


def test_render_has_boundaries_ids_counters_tags():
    pb = Playbook()
    a = pb.add_bullet("strategy one", "strategies_and_hard_rules", tags=[HELPFUL])
    a.mark_helpful(); a.mark_harmful()
    out = pb.render()
    assert out.startswith("=== PLAYBOOK (ACE) BEGIN ===") and out.rstrip().endswith("=== PLAYBOOK (ACE) END ===")
    assert "## strategies_and_hard_rules" in out
    assert f"[{a.id}]" in out and "(↑1 ↓1)" in out and "#helpful" in out and "strategy one" in out
    # C2 #906: the STABLE `[id] content #tags` prefix precedes the MUTABLE counters (KV-cache stable prefix)
    line = next(ln for ln in out.splitlines() if f"[{a.id}]" in ln)
    assert line.index("strategy one") < line.index("(↑1 ↓1)")


def test_render_empty_playbook_is_just_boundaries():
    out = Playbook().render().splitlines()
    assert out == ["=== PLAYBOOK (ACE) BEGIN ===", "=== PLAYBOOK (ACE) END ==="]


def test_json_round_trip_is_lossless():
    pb = Playbook()
    a = pb.add_bullet("a", "strategies_and_hard_rules", tags=[HELPFUL, "x"])
    a.mark_helpful()
    pb.add_bullet("b", "apis_to_use")
    pb.add_bullet("c", "formulas_and_calculations")
    rt = Playbook.from_json(pb.to_json())
    assert rt.to_dict() == pb.to_dict()                              # full structural equality
    assert rt.schema_version == SCHEMA_VERSION and rt._seq == pb._seq
    got = rt.get("b-0")
    assert got.helpful_count == 1 and got.tags == [HELPFUL, "x"]     # counters + tags preserved


def test_reload_continues_minting_without_collision():
    pb = Playbook()
    pb.add_bullet("a", "apis_to_use")        # b-0
    pb.add_bullet("b", "apis_to_use")        # b-1
    rt = Playbook.from_json(pb.to_json())
    new = rt.add_bullet("c", "apis_to_use")  # must NOT be b-0/b-1
    assert new.id == "b-2" and len({x.id for x in rt.bullets()}) == 3


def test_from_dict_recovers_seq_if_counter_lagged():
    # a hand-written/older store whose seq is behind the max id must not re-mint a colliding id.
    d = {"schema_version": 1, "seq": 0, "sections": {"apis_to_use": [
        {"id": "b-5", "content": "x", "section": "apis_to_use"}]}}
    pb = Playbook.from_dict(d)
    assert pb.add_bullet("y", "apis_to_use").id == "b-6"


def test_from_json_rejects_newer_schema_version_fail_closed():
    d = {"schema_version": SCHEMA_VERSION + 1, "seq": 0, "sections": {}}
    with pytest.raises(ValueError):
        Playbook.from_json(json.dumps(d))


@pytest.mark.parametrize("bad", ["not json", "[]", json.dumps({"sections": {"s": "notalist"}}),
                                 json.dumps({"sections": {"s": [{"content": "x"}]}})])
def test_from_json_garbage_raises_value_error(bad):
    with pytest.raises(ValueError):
        Playbook.from_json(bad)


def test_default_sections_are_the_four_canonical():
    assert DEFAULT_SECTIONS == ("strategies_and_hard_rules", "apis_to_use",
                                "verification_checklist", "formulas_and_calculations")
