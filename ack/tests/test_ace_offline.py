"""ACE-ADAPT-OFFLINE (#855 / #864): the offline batch build. Pins A-003 (configurable batch size),
C-003 (parallel deterministic delta merge), G-001 (offline build + test-split pass@1), G-003 (multi-epoch),
G-004 (ledger-replay warmup for the online loop).
"""
from __future__ import annotations

import json

from ack.ace import (Playbook, Trajectory, Delta, DeltaOp, OP_ADD, OP_RATE,
                     OfflineConfig, Sample, merge_deltas, build_offline, evaluate, warmup,
                     OnlineAdapter, HELPFUL)


def _chat(*insights):
    payload = json.dumps({"insights": list(insights), "ratings": []})
    return lambda prompt: payload


# ─── C-003: parallel deterministic merge ─────────────────────────────────────────────────────────────
def test_merge_deltas_is_input_order_and_dedups_adds_last_wins():
    d1 = Delta(operations=[DeltaOp(op=OP_ADD, section="apis_to_use", content="use the batch API"),
                           DeltaOp(op=OP_RATE, bullet_id="b-0", verdict=HELPFUL)])
    d2 = Delta(operations=[DeltaOp(op=OP_ADD, section="apis_to_use", content="use the batch API", tags=["v2"]),
                           DeltaOp(op=OP_ADD, section="apis_to_use", content="paginate results")])
    merged = merge_deltas([d1, None, d2])
    adds = [o for o in merged.operations if o.op == OP_ADD]
    assert [o.content for o in adds] == ["use the batch API", "paginate results"]   # deduped, input order
    assert adds[0].tags == ["v2"]                                                   # last-wins on the collision
    assert sum(1 for o in merged.operations if o.op == OP_RATE) == 1                # RATE accumulated


def test_merge_deltas_empty_is_empty():
    assert merge_deltas([None, Delta(), Delta()]).is_empty()


# ─── A-003 + G-001: offline build + test-split pass@1 ────────────────────────────────────────────────
def test_build_offline_optimizes_then_evaluates_on_test_split():
    pb = Playbook()
    train = [Sample(query="add(a,b)", expected="a+b"), Sample(query="mul(a,b)", expected="a*b")]
    # a label-free run: the model "executes" and returns an output (the expected label is NOT used to adapt)
    run = lambda q, ctx: "computed"
    rep = build_offline(train, pb, chat=_chat({"content": "validate inputs first",
                                               "section": "verification_checklist"}),
                        run=run, config=OfflineConfig(max_epochs=1))
    assert rep["samples_seen"] == 2 and rep["added"] >= 1 and len(pb) >= 1
    # G-001: evaluate on a held-out test split with pass@1 — uses ONLY the playbook (no train data)
    test = [Sample(query="add(1,2)", expected="3"), Sample(query="sub(5,2)", expected="WRONG")]
    ev = evaluate(pb, test, run=lambda q, ctx: "3")
    assert ev["n"] == 2 and ev["passed"] == 1 and ev["accuracy"] == 0.5            # pass@1


def test_batch_size_configurable_and_base_is_one():
    pb = Playbook()
    samples = [Sample(query=f"q{i}") for i in range(4)]
    rep = build_offline(samples, pb, chat=_chat({"content": "x", "section": "apis_to_use"}),
                        run=lambda q, ctx: "out", config=OfflineConfig(batch_size=2, max_epochs=1))
    assert rep["batches"] == 2 and rep["samples_seen"] == 4                        # 4 samples / batch 2
    rep1 = build_offline([Sample(query="q")], Playbook(),
                         chat=_chat({"content": "y", "section": "apis_to_use"}),
                         run=lambda q, ctx: "out", config=OfflineConfig(batch_size=1, max_epochs=1))
    assert rep1["batches"] == 1                                                    # base config = 1


# ─── G-003: multi-epoch ──────────────────────────────────────────────────────────────────────────────
def test_multi_epoch_revisits_samples_and_default_is_five():
    assert OfflineConfig().max_epochs == 5
    pb = Playbook()
    rep = build_offline([Sample(query="q1"), Sample(query="q2")], pb,
                        chat=_chat({"content": "lesson", "section": "apis_to_use"}),
                        run=lambda q, ctx: "out", config=OfflineConfig(max_epochs=3))
    assert rep["epochs_run"] == 3 and rep["samples_seen"] == 6                     # 2 samples × 3 epochs


# ─── parallel map_fn + budget + fail-soft ────────────────────────────────────────────────────────────
def test_map_fn_can_parallelize_and_result_is_deterministic():
    calls = []
    def map_fn(fn, batch):
        return [fn(s) for s in batch]                                             # a stand-in parallel map
    pb = Playbook()
    build_offline([Sample(query="q1"), Sample(query="q2")], pb, chat=_chat({"content": "z",
                  "section": "apis_to_use"}), run=lambda q, ctx: calls.append(q) or "o",
                  config=OfflineConfig(batch_size=2, max_epochs=1), map_fn=map_fn)
    assert len(pb) == 1                                                            # both deltas merged → 1 bullet


class _Budget:
    def __init__(self, allow): self.allow, self.charged = allow, 0
    def can_afford(self, c): return self.allow
    def charge(self, c): self.charged += c


def test_budget_gate_stops_the_build():
    pb = Playbook()
    rep = build_offline([Sample(query="q")], pb, chat=_chat({"content": "x", "section": "apis_to_use"}),
                        run=lambda q, ctx: "o", budget=_Budget(allow=False))
    assert rep["added"] == 0 and len(pb) == 0                                      # unaffordable → no build


def test_build_is_fail_soft_on_a_bad_run():
    pb = Playbook()
    def boom(q, ctx): raise RuntimeError("model down")
    rep = build_offline([Sample(query="q")], pb, chat=_chat({"content": "x", "section": "apis_to_use"}),
                        run=boom, config=OfflineConfig(max_epochs=1))             # never raises
    assert rep["skipped"] >= 1 and len(pb) == 0


# ─── G-004: offline warmup → online continues on the same playbook ───────────────────────────────────
def test_warmup_replays_ledger_then_online_builds_on_it():
    pb = Playbook()
    ledger = [Trajectory(query="past task A", outcome="success"),
              Trajectory(query="past task B", outcome="success")]
    rep = warmup(ledger, pb, chat=_chat({"content": "warmup lesson", "section": "strategies_and_hard_rules"}),
                 config=OfflineConfig(max_epochs=1))
    assert rep["samples_seen"] == 2 and "warmup lesson" in pb.render()            # seeded from the ledger
    seeded = len(pb)
    # G-004: the online loop continues on the SAME playbook
    adapter = OnlineAdapter(pb, chat=_chat({"content": "online lesson", "section": "apis_to_use"}))
    adapter.adapt(Trajectory(query="live task", outcome="success"))
    assert len(pb) > seeded and "online lesson" in pb.render()                    # online built on the warmup
