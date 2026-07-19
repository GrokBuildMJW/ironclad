"""ACE-ADAPT-ONLINE (#855 / #862): the online loop composition + budget gate + async reflection worker.
Pins G-002 (online adaptation), O-001 (label-free), O-002 (cumulative), L-001 (reflection rounds).
"""
from __future__ import annotations

import json
import threading
import time

from ack.ace import (Playbook, Trajectory, AdaptConfig, adapt_once, OnlineAdapter, ReflectionWorker,
                     HELPFUL, HARMFUL)


def _chat(insights, ratings):
    payload = json.dumps({"insights": insights, "ratings": ratings})
    calls = {"n": 0}

    def chat(prompt: str) -> str:
        calls["n"] += 1
        return payload
    return chat, calls


class _Budget:
    def __init__(self, allow=True, raise_afford=False):
        self.allow, self.raise_afford, self.charged = allow, raise_afford, 0

    def can_afford(self, cost):
        if self.raise_afford:
            raise RuntimeError("ledger down")
        return self.allow

    def charge(self, cost):
        self.charged += cost


def _traj():
    return Trajectory(query="add(a,b)", outcome="success", used_bullet_ids=["b-0"])


def test_adapt_once_composes_reflect_curate_apply_refine():
    pb = Playbook()
    a = pb.add_bullet("seed strategy", "strategies_and_hard_rules")          # b-0 (used)
    chat, calls = _chat([{"content": "write the test first", "section": "strategies_and_hard_rules"}],
                        [{"bullet_id": "b-0", "verdict": HELPFUL}])
    s = adapt_once(_traj(), pb, chat=chat)
    assert calls["n"] == 1 and s["reflected"] == 1 and s["added"] == 1 and s["rated"] == 1
    assert a.helpful_count == 1 and HELPFUL in a.tags                        # rating applied to the used bullet
    assert len(pb) == 2                                                      # seed + 1 new insight (cumulative)


def _contradiction_and_noise():
    pb = Playbook()
    keep = pb.add_bullet("always validate the parser input", "strategies_and_hard_rules"); keep.mark_helpful()
    lose = pb.add_bullet("never validate the parser input", "strategies_and_hard_rules")   # net 0 < keep's 1
    noisy = pb.add_bullet("a harmful dominant rule", "apis_to_use"); noisy.mark_harmful()   # net -1
    return pb, keep, lose, noisy


def test_robust_pass_resolves_contradictions_and_quarantines_noise():
    # #914: the K-002/K-003 robustness half now runs inside the live adapt loop (was orphaned)
    pb, keep, lose, noisy = _contradiction_and_noise()
    chat, _ = _chat([{"content": "a fresh unrelated lesson", "section": "useful_context"}], [])
    s = adapt_once(Trajectory(query="q", outcome="success"), pb, chat=chat, config=AdaptConfig(robust=True))
    assert s["resolved"] >= 1 and s["quarantined"] >= 1
    assert pb.get(lose.id) is None                       # K-003: the lower-utility contradicted belief dropped
    assert pb.get(noisy.id) is None                      # K-002: the net-negative bullet quarantined
    assert pb.get(keep.id) is not None                   # the trusted belief + fresh lessons survive


def test_robust_pass_can_be_disabled():
    pb, keep, lose, noisy = _contradiction_and_noise()
    chat, _ = _chat([{"content": "a fresh unrelated lesson", "section": "useful_context"}], [])
    s = adapt_once(Trajectory(query="q", outcome="success"), pb, chat=chat, config=AdaptConfig(robust=False))
    assert s["resolved"] == 0 and s["quarantined"] == 0
    assert pb.get(lose.id) is not None and pb.get(noisy.id) is not None   # untouched by robustness when off


def test_budget_gate_skips_without_calling_the_model():
    pb = Playbook()
    chat, calls = _chat([{"content": "x", "section": "apis_to_use"}], [])
    s = adapt_once(_traj(), pb, chat=chat, budget=_Budget(allow=False))
    assert s["skipped"] is True and calls["n"] == 0 and len(pb) == 0        # no LLM call, no mutation


def test_budget_charged_on_a_productive_adapt():
    pb = Playbook()
    b = _Budget(allow=True)
    chat, _ = _chat([{"content": "x", "section": "apis_to_use"}], [])
    s = adapt_once(_traj(), pb, chat=chat, budget=b, config=AdaptConfig(cost=3))
    assert s["charged"] == 3 and b.charged == 3 and s["added"] == 1


def test_flaky_budget_is_fail_soft_skip():
    pb = Playbook()
    chat, calls = _chat([{"content": "x", "section": "apis_to_use"}], [])
    s = adapt_once(_traj(), pb, chat=chat, budget=_Budget(raise_afford=True))
    assert s["skipped"] is True and calls["n"] == 0


def test_empty_reflection_is_noop_and_uncharged():
    pb = Playbook()
    b = _Budget(allow=True)

    def chat(prompt):
        return "no json here at all"
    s = adapt_once(_traj(), pb, chat=chat, budget=b)
    assert s["added"] == 0 and s["charged"] == 0 and b.charged == 0 and len(pb) == 0


def test_cumulative_across_runs():
    pb = Playbook()
    chat, _ = _chat([{"content": "lesson A", "section": "apis_to_use"}], [])
    adapt_once(Trajectory(query="q1", outcome="ok"), pb, chat=chat)
    adapt_once(Trajectory(query="q2", outcome="ok"), pb, chat=chat)
    assert len(pb) == 2                                                      # O-002: the same playbook grows


def test_online_adapter_binds_and_delegates():
    pb = Playbook()
    chat, _ = _chat([{"content": "x", "section": "apis_to_use"}], [])
    ad = OnlineAdapter(pb, chat=chat)
    assert ad.adapt(Trajectory(query="q", outcome="ok"))["added"] == 1 and len(pb) == 1


def test_worker_submit_is_nonblocking_and_process_pending_drains():
    seen = []
    w = ReflectionWorker(process=lambda t: seen.append(t.query))
    assert w.submit(Trajectory(query="t1")) is True and w.pending() == 1
    w.submit(Trajectory(query="t2"))
    assert w.process_pending() == 2 and seen == ["t1", "t2"] and w.processed == 2


def test_worker_is_fail_soft_on_a_bad_item():
    def boom(t):
        raise RuntimeError("reflection blew up")
    w = ReflectionWorker(process=boom)
    w.submit(Trajectory(query="x"))
    w.process_pending()
    assert w.errors == 1 and w.processed == 0                               # survives, counts the error


def test_worker_drops_when_queue_full_rather_than_block():
    w = ReflectionWorker(process=lambda t: None, max_queue=1)
    assert w.submit(Trajectory(query="a")) is True
    assert w.submit(Trajectory(query="b")) is False and w.dropped == 1      # hot-path-safe: drop, don't block


def test_worker_background_thread_processes_then_stops():
    seen = []
    w = ReflectionWorker(process=lambda t: seen.append(t.query))
    w.start()
    try:
        w.submit(Trajectory(query="bg"))
        for _ in range(50):                                                 # poll up to ~1s for the daemon
            if w.processed >= 1:
                break
            time.sleep(0.02)
    finally:
        w.stop()
    assert seen == ["bg"] and w.processed == 1


def test_worker_stop_waits_for_inflight_item_and_clears_thread():
    started = threading.Event()
    finish = threading.Event()
    completed = threading.Event()

    def process(_item):
        started.set()
        assert finish.wait(timeout=1.0)
        completed.set()

    w = ReflectionWorker(process=process)
    w.submit(Trajectory(query="in-flight"))
    w.start()
    release = None
    try:
        assert started.wait(timeout=1.0)
        release = threading.Timer(0.05, finish.set)
        release.start()
        w.stop()
    finally:
        finish.set()
        if release is not None:
            release.join(timeout=1.0)
        if w._thread is not None:
            w.stop()
    assert completed.is_set() and w.processed == 1
    assert w._thread is None
    assert not any(t.name == "ace-reflection-worker" and t.is_alive() for t in threading.enumerate())


def test_worker_stop_finite_timeout_keeps_live_thread_observable():
    started = threading.Event()
    finish = threading.Event()

    def process(_item):
        started.set()
        assert finish.wait(timeout=1.0)

    w = ReflectionWorker(process=process)
    w.submit(Trajectory(query="in-flight"))
    w.start()
    try:
        assert started.wait(timeout=1.0)
        w.stop(timeout=0.01)
        assert w._thread is not None and w._thread.is_alive()
    finally:
        finish.set()
        w.stop()
    assert w._thread is None and w.processed == 1


def test_worker_stop_drains_items_not_yet_started():
    started = threading.Event()
    finish = threading.Event()
    seen = []

    def process(item):
        seen.append(item.query)
        started.set()
        assert finish.wait(timeout=1.0)

    w = ReflectionWorker(process=process)
    for query in ("first", "queued-1", "queued-2"):
        w.submit(Trajectory(query=query))
    w.start()
    release = None
    try:
        assert started.wait(timeout=1.0)
        release = threading.Timer(0.05, finish.set)
        release.start()
        w.stop()
    finally:
        finish.set()
        if release is not None:
            release.join(timeout=1.0)
        if w._thread is not None:
            w.stop()
    assert seen == ["first"] and w.processed == 1
    assert w.pending() == 0 and w._thread is None
