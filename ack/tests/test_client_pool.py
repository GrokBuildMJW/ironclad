"""Client-side code-agent parallelism (core/engine/client.py).

The thin client runs ``claude --print`` for staged handovers in a bounded pool. These
tests validate the pool semantics WITHOUT launching claude (the handover runner is
stubbed): real concurrency (N agents run at once), claim-once (no double launch across
polls), and unclaim-on-failure (a failed task is retried next poll).
"""
from __future__ import annotations

import sys
import threading
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path

# core/engine on sys.path so `import client` works (conftest adds core/). The client
# is pure stdlib (no gx10 / openai import), so nothing heavy needs stubbing.
_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import client  # noqa: E402
import pytest  # noqa: E402


class _FakeServer:
    def __init__(self, pending):
        self._pending = pending
        self.uploaded = []
        self._lock = threading.Lock()

    def pending(self):
        return list(self._pending)

    def feedback(self, task_id, agent, content):
        with self._lock:
            self.uploaded.append((task_id, agent, content))
        return {"feedback_file": f"summaries/feedback/{task_id}_{agent}-feedback.md"}


def _items(*ids):
    return [{"id": t, "agent": "OPUS", "title": t, "type": "feature"} for t in ids]


def test_dispatch_claims_and_runs_each_once(monkeypatch, tmp_path):
    srv = _FakeServer(_items("KGC-1", "KGC-2"))
    ran = []
    monkeypatch.setattr(client, "_run_handover",
                        lambda item, codedir, log=print: f"fb-{item['id']}" if ran.append(item["id"]) or True else None)
    claimed: set = set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futs = client.dispatch_pending(srv, tmp_path, pool, claimed)
        wait(futs)
    assert sorted(ran) == ["KGC-1", "KGC-2"]
    assert claimed == {"KGC-1", "KGC-2"}
    assert sorted(u[0] for u in srv.uploaded) == ["KGC-1", "KGC-2"]


def test_already_claimed_not_resubmitted(monkeypatch, tmp_path):
    srv = _FakeServer(_items("KGC-1"))
    calls = []
    monkeypatch.setattr(client, "_run_handover",
                        lambda item, codedir, log=print: calls.append(item["id"]) or "fb")
    claimed = {"KGC-1"}  # bereits in Arbeit
    with ThreadPoolExecutor(max_workers=2) as pool:
        futs = client.dispatch_pending(srv, tmp_path, pool, claimed)
        wait(futs)
    assert futs == [] and calls == []  # nichts neu gestartet


def test_runs_concurrently(monkeypatch, tmp_path):
    """3 handovers in a size-3 pool must run at the same time, not serially."""
    barrier = threading.Barrier(3, timeout=5)
    started = []

    def _blocking(item, codedir, log=print):
        started.append(item["id"])
        barrier.wait()  # alle drei müssen hier zusammentreffen → echte Parallelität
        return f"fb-{item['id']}"

    monkeypatch.setattr(client, "_run_handover", _blocking)
    srv = _FakeServer(_items("KGC-1", "KGC-2", "KGC-3"))
    claimed: set = set()
    with ThreadPoolExecutor(max_workers=3) as pool:
        futs = client.dispatch_pending(srv, tmp_path, pool, claimed)
        done, _ = wait(futs, timeout=8)
    assert len(done) == 3
    assert len(srv.uploaded) == 3  # barrier nur erreichbar wenn alle gleichzeitig liefen


def test_failure_unclaims_for_retry(monkeypatch, tmp_path):
    srv = _FakeServer(_items("KGC-1"))
    monkeypatch.setattr(client, "_run_handover", lambda item, codedir, log=print: None)  # kein Feedback
    claimed: set = set()
    with ThreadPoolExecutor(max_workers=1) as pool:
        wait(client.dispatch_pending(srv, tmp_path, pool, claimed))
    assert claimed == set()       # freigegeben → nächster Poll versucht erneut
    assert srv.uploaded == []     # nichts hochgeladen


def test_exception_unclaims(monkeypatch, tmp_path):
    srv = _FakeServer(_items("KGC-1"))

    def _boom(item, codedir, log=print):
        raise RuntimeError("claude crashed")

    monkeypatch.setattr(client, "_run_handover", _boom)
    claimed: set = set()
    with ThreadPoolExecutor(max_workers=1) as pool:
        results = wait(client.dispatch_pending(srv, tmp_path, pool, claimed))
    assert claimed == set()
    # der Job darf NICHT die ganze Schleife killen — _process_one fängt ab
    assert all(f.result() is False for f in results.done)
