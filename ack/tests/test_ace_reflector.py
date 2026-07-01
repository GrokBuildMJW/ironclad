"""ACE-REFLECT (#855 / #858): the Reflector — trajectory analysis, iterative refinement, label-free,
bullet rating, structured I/O. Transport-injected (a fake ``chat``); fail-soft (never raises).
"""
from __future__ import annotations

import json

from ack.ace import Bullet, Trajectory, ReflectorOutput, reflect, HELPFUL, HARMFUL


def _chat_returning(*responses):
    """A fake ``chat`` that returns the queued responses in order (last repeats); records the prompts."""
    calls = {"prompts": []}
    seq = list(responses)

    def chat(prompt: str) -> str:
        calls["prompts"].append(prompt)
        return seq[min(len(calls["prompts"]) - 1, len(seq) - 1)]
    return chat, calls


def _traj():
    return Trajectory(query="add(a,b)", steps=["wrote add", "ran test"], outcome="success",
                      used_bullet_ids=["b-0", "b-1"])


def test_trajectory_analysis_parses_insights_and_ratings():
    resp = json.dumps({"insights": [{"content": "write the test first", "section": "strategies_and_hard_rules",
                                     "tags": [HELPFUL]}],
                       "ratings": [{"bullet_id": "b-0", "verdict": "helpful"},
                                   {"bullet_id": "b-1", "verdict": "harmful"}]})
    chat, calls = _chat_returning(resp)
    out = reflect(_traj(), chat=chat, used_bullets=[Bullet("b-0", "x", "s"), Bullet("b-1", "y", "s")])
    assert len(calls["prompts"]) == 1 and out.rounds_run == 1
    assert len(out.insights) == 1 and out.insights[0].content == "write the test first"
    assert out.insights[0].section == "strategies_and_hard_rules" and out.insights[0].tags == [HELPFUL]
    assert {(r.bullet_id, r.verdict) for r in out.ratings} == {("b-0", "helpful"), ("b-1", "harmful")}


def test_label_free_uses_outcome_not_a_label():
    # E-003: the only signal is the free-text outcome; the analysis prompt carries it (no label parameter).
    chat, calls = _chat_returning('{"insights":[],"ratings":[]}')
    reflect(Trajectory(query="q", outcome="failed: timeout on tool X"), chat=chat)
    assert "failed: timeout on tool X" in calls["prompts"][0] and "OUTCOME" in calls["prompts"][0]


def test_invalid_ratings_and_blank_insights_are_dropped():
    resp = json.dumps({"insights": [{"content": "", "section": "apis_to_use"},          # blank → drop
                                    {"content": "ok", "section": "weird-section"}],      # bad section → snap
                       "ratings": [{"bullet_id": "b-0", "verdict": "bogus"},             # bad verdict → drop
                                   {"bullet_id": "", "verdict": "helpful"},              # no id → drop
                                   {"bullet_id": "b-0", "verdict": "helpful"},
                                   {"bullet_id": "b-0", "verdict": "harmful"}]})         # dup id → drop 2nd
    chat, _ = _chat_returning(resp)
    out = reflect(_traj(), chat=chat)
    assert len(out.insights) == 1 and out.insights[0].section == "strategies_and_hard_rules"  # snapped to first
    assert [(r.bullet_id, r.verdict) for r in out.ratings] == [("b-0", "helpful")]


def test_iterative_refinement_runs_extra_rounds_and_replaces_insights():
    r1 = json.dumps({"insights": [{"content": "draft one", "section": "apis_to_use"},
                                  {"content": "draft two", "section": "apis_to_use"}], "ratings": []})
    r2 = json.dumps({"insights": [{"content": "merged sharp insight", "section": "apis_to_use"}]})
    chat, calls = _chat_returning(r1, r2)
    out = reflect(_traj(), chat=chat, rounds=2)
    assert len(calls["prompts"]) == 2 and out.rounds_run == 2 and "Refine" in calls["prompts"][1]
    assert len(out.insights) == 1 and out.insights[0].content == "merged sharp insight"


def test_fail_soft_on_transport_error_is_empty():
    def boom(prompt):
        raise RuntimeError("model down")
    out = reflect(_traj(), chat=boom)
    assert out.is_empty() and out.rounds_run == 0


def test_fail_soft_on_unparseable_output_is_empty_insights():
    chat, _ = _chat_returning("I think you did great! no json here.")
    out = reflect(_traj(), chat=chat)
    assert out.is_empty() and out.rounds_run == 1     # called the model, parsed nothing


def test_json_embedded_in_prose_is_extracted():
    chat, _ = _chat_returning('Sure! Here you go:\n{"insights":[{"content":"c","section":"apis_to_use"}],"ratings":[]}\nHope that helps.')
    out = reflect(_traj(), chat=chat)
    assert len(out.insights) == 1 and out.insights[0].content == "c"


def test_io_round_trip():
    t = _traj()
    assert Trajectory.from_dict(t.to_dict()).to_dict() == t.to_dict()
    out = ReflectorOutput(insights=[], ratings=[], rounds_run=1)
    assert out.to_dict() == {"insights": [], "ratings": [], "rounds_run": 1}