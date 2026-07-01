"""ACE-GROW (#855 / #860): grow-and-refine — semantic de-dup (injected embedder) + lexical fallback +
utility pruning + lazy/proactive refinement. Pure / stdlib-only.

Pins D-001 (dedup), D-002 (lazy vs proactive), D-003 (prune on overflow), L-003 (threshold), L-004 (trigger).
"""
from __future__ import annotations

from ack.ace import Playbook, dedupe, prune, refine, cosine, lexical_sim, HELPFUL


def test_cosine_and_lexical_basics():
    assert cosine([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert cosine([1.0, 0.0], [0.0, 1.0]) == 0.0
    assert cosine([], [1.0]) == 0.0                       # mismatched/empty → 0, never raises
    assert lexical_sim("read the file", "read the file") == 1.0
    assert 0.0 < lexical_sim("read the file first", "read the file") < 1.0
    assert lexical_sim("alpha beta", "gamma delta") == 0.0


def _embed_map(mapping):
    return lambda texts: [mapping[t] for t in texts]


def test_semantic_dedup_merges_within_section_summing_counters_and_tags():
    pb = Playbook()
    a = pb.add_bullet("write the test first", "strategies_and_hard_rules", tags=[HELPFUL]); a.mark_helpful()
    b = pb.add_bullet("always write a test up front", "strategies_and_hard_rules", tags=["x"]); b.mark_helpful()
    c = pb.add_bullet("unrelated rule", "strategies_and_hard_rules")
    embed = _embed_map({"write the test first": [1.0, 0.0], "always write a test up front": [0.99, 0.01],
                        "unrelated rule": [0.0, 1.0]})
    merged = dedupe(pb, embed=embed, threshold=0.9)
    assert merged == 1 and len(pb) == 2                    # a+b merged, c kept
    kept = pb.get(a.id)
    assert kept is not None and kept.helpful_count == 2    # counters summed
    assert "x" in kept.tags and HELPFUL in kept.tags       # tags unioned
    assert pb.get(b.id) is None and pb.get(c.id) is not None


def test_dedup_is_per_section_not_cross_section():
    pb = Playbook()
    pb.add_bullet("same text", "apis_to_use")
    pb.add_bullet("same text", "formulas_and_calculations")
    embed = _embed_map({"same text": [1.0, 0.0]})
    assert dedupe(pb, embed=embed, threshold=0.9) == 0 and len(pb) == 2   # different sections → not dupes


def test_lexical_fallback_when_no_embedder():
    pb = Playbook()
    pb.add_bullet("read the file before editing", "apis_to_use")
    pb.add_bullet("read the file before editing", "apis_to_use")          # identical → Jaccard 1.0
    pb.add_bullet("a totally different concept", "apis_to_use")
    assert dedupe(pb, threshold=0.9) == 1 and len(pb) == 2


def test_dedup_fail_soft_falls_back_to_lexical_on_embedder_error():
    pb = Playbook()
    pb.add_bullet("identical content", "apis_to_use")
    pb.add_bullet("identical content", "apis_to_use")
    def boom(texts):
        raise RuntimeError("embedder down")
    assert dedupe(pb, embed=boom, threshold=0.9) == 1     # error → lexical floor still merges the exact dupe


def test_prune_removes_lowest_utility_first_over_count_budget():
    pb = Playbook()
    keep = pb.add_bullet("valuable", "apis_to_use"); keep.mark_helpful(); keep.mark_helpful()
    mid = pb.add_bullet("neutralish", "apis_to_use")
    bad = pb.add_bullet("harmful one", "apis_to_use"); bad.mark_harmful()
    pruned = prune(pb, max_bullets=2)
    assert pruned == 1 and pb.get(bad.id) is None          # lowest net_utility (-1) pruned first
    assert pb.get(keep.id) is not None and pb.get(mid.id) is not None


def test_prune_tiebreak_evicts_oldest_not_newest():
    # C2 #902 regression: on a net_utility + harmful_count tie the victim must be the OLDEST bullet (smallest
    # seq, D-003), never the newest — a `-_seq` tiebreak wrongly evicted the freshest lesson.
    pb = Playbook()
    oldest = pb.add_bullet("first added", "apis_to_use")
    mid = pb.add_bullet("second added", "apis_to_use")
    newest = pb.add_bullet("third added", "apis_to_use")
    assert prune(pb, max_bullets=2) == 1
    assert pb.get(oldest.id) is None                       # the oldest is evicted on the tie
    assert pb.get(mid.id) is not None and pb.get(newest.id) is not None


def test_prune_on_rendered_size_budget():
    pb = Playbook()
    for i in range(10):
        pb.add_bullet(f"bullet number {i} with some text", "apis_to_use")
    before = len(pb)
    pruned = prune(pb, max_chars=200)
    assert pruned > 0 and len(pb) == before - pruned and len(pb.render()) <= 200 + 0  # within budget


def test_refine_lazy_is_noop_within_budget_but_runs_over_budget():
    pb = Playbook()
    pb.add_bullet("one", "apis_to_use")
    r1 = refine(pb, max_bullets=5, lazy=True)
    assert r1["ran"] is False and r1["merged"] == 0 and r1["pruned"] == 0
    for i in range(6):
        pb.add_bullet(f"extra {i}", "apis_to_use")
    r2 = refine(pb, max_bullets=5, lazy=True)
    assert r2["ran"] is True and r2["pruned"] >= 1 and len(pb) <= 5


def test_refine_proactive_always_dedupes_and_prunes():
    pb = Playbook()
    pb.add_bullet("dup text", "apis_to_use")
    pb.add_bullet("dup text", "apis_to_use")
    r = refine(pb, dedup_threshold=0.9, lazy=False)        # no embed → lexical; no budget → just dedup
    assert r["ran"] is True and r["merged"] == 1 and len(pb) == 1