"""ACE-GEN (#855 / #861): Generator integration — relevant-bullet retrieval (semantic + lexical),
playbook-guided context, bullet-id tracking, output→trajectory closure. Pure / stdlib-only.

Pins H-001 (playbook-guided inference), H-002 (bullet-id tracking), N-004 (Generator output → Reflector).
"""
from __future__ import annotations

from ack.ace import (Playbook, GeneratorContext, select_relevant, prepare_context, to_trajectory,
                     Trajectory, DEFAULT_TOP_K)


def _pb():
    pb = Playbook()
    pb.add_bullet("use the email API to send mail", "apis_to_use")          # b-0
    pb.add_bullet("always write a pytest first", "strategies_and_hard_rules")  # b-1
    pb.add_bullet("compute interest with the XBRL formula", "formulas_and_calculations")  # b-2
    return pb


def _embed_map(mapping):
    return lambda texts: [mapping[t] for t in texts]


def test_semantic_retrieval_ranks_relevant_bullet_first():
    pb = _pb()
    # query close to the email-API bullet; orthogonal to the others
    embed = _embed_map({
        "send an email to the user": [1.0, 0.0, 0.0],
        "use the email API to send mail": [0.98, 0.02, 0.0],
        "always write a pytest first": [0.0, 1.0, 0.0],
        "compute interest with the XBRL formula": [0.0, 0.0, 1.0],
    })
    out = select_relevant(pb, "send an email to the user", embed=embed, top_k=2)
    assert [b.id for b in out][0] == "b-0" and len(out) == 2


def test_lexical_fallback_when_no_embedder():
    pb = _pb()
    out = select_relevant(pb, "write a pytest for the function", top_k=1)   # token overlap → the pytest bullet
    assert len(out) == 1 and out[0].id == "b-1"


def test_top_k_and_threshold_bound_the_subset():
    pb = _pb()
    assert len(select_relevant(pb, "anything", top_k=2)) == 2               # capped to top_k
    # a high threshold with no lexical overlap → nothing selected
    assert select_relevant(pb, "zzz qqq", top_k=5, threshold=0.5) == []


def test_retrieval_fail_soft_on_embedder_error():
    pb = _pb()
    def boom(texts):
        raise RuntimeError("embedder down")
    out = select_relevant(pb, "write a pytest first", embed=boom, top_k=1)  # error → lexical → still ranks
    assert len(out) == 1 and out[0].id == "b-1"


def test_prepare_context_renders_subset_and_tracks_ids():
    pb = _pb()
    ctx = prepare_context(pb, "write a pytest first", top_k=1)
    assert isinstance(ctx, GeneratorContext) and ctx.bullet_ids == ["b-1"]   # H-002 tracking
    assert "[b-1]" in ctx.text and "always write a pytest first" in ctx.text
    assert ctx.text.startswith("=== PLAYBOOK (ACE, relevant subset) BEGIN ===")  # H-001 injected context


def test_empty_playbook_yields_empty_context():
    ctx = prepare_context(Playbook(), "q")
    assert ctx.is_empty() and ctx.text == "" and ctx.bullet_ids == []


def test_to_trajectory_defaults_used_ids_to_injected_context():
    pb = _pb()
    ctx = prepare_context(pb, "send an email", top_k=2)
    traj = to_trajectory("send an email", steps=["called email API"], outcome="success", context=ctx)
    assert isinstance(traj, Trajectory) and traj.used_bullet_ids == ctx.bullet_ids   # N-004 → N-001 closure
    assert traj.query == "send an email" and traj.outcome == "success"


def test_to_trajectory_explicit_used_ids_override_context():
    traj = to_trajectory("q", outcome="failure", used_bullet_ids=["b-9"])
    assert traj.used_bullet_ids == ["b-9"]