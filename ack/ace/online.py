"""ACE online adaptation — the closed Generator → Reflector → Curator → grow-and-refine loop, run on an
async background worker, budget-gated.

Epic #855 cluster ACE-ADAPT-ONLINE (catalogue G-002 online adaptation, O-001 label-free, O-002 cumulative
learning, L-001 reflection rounds). This composes the per-cluster pieces into one step:

    Trajectory  ──reflect──▶ ReflectorOutput ──curate──▶ Delta ──apply_delta──▶ Playbook ──refine──▶

`adapt_once` is the pure composition (budget-gated, fail-soft). `ReflectionWorker` is the async seam: the
engine's `post_feedback` hook only **submits** a trajectory (an O(1), non-blocking enqueue — hot-path-safe,
the C0 correctness requirement), and a background daemon drains the queue off the turn path. Budget is an
INJECTED ledger (duck-typed `can_afford`/`charge`, like `ack.verify.verify_with_judge`); the chat + embedder
are injected too — so this module stays pure / stdlib-only and is unit-tested with fakes. The real engine
wiring (the hook → `submit`, the orchestrator-model `chat`, the `/embed` embedder) is ACE-WIRE (#863).

LABEL-FREE (O-001): the only signal is `Trajectory.outcome`. CUMULATIVE (O-002): the same `Playbook` is
mutated across runs. FAIL-SOFT throughout — a transport/parse error degrades to a no-op, never raises.
"""
from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from typing import Callable, List, Optional

from .playbook import Playbook
from .reflector import Trajectory, reflect
from .curator import curate, apply_delta
from .grow import refine, DEFAULT_DEDUP_THRESHOLD


@dataclass
class AdaptConfig:
    """Online-adaptation hyperparameters. Product defaults are conservative (the paper's larger counts are
    the offline-research setting): one reflection round, lazy refine, no LLM cost unless a budget is wired."""

    rounds: int = 1                                   # L-001 reflection rounds (>=1)
    dedup_threshold: float = DEFAULT_DEDUP_THRESHOLD
    max_bullets: Optional[int] = None
    max_chars: Optional[int] = None
    refine_lazy: bool = True                          # D-002: refine only when over budget by default
    cost: int = 1                                     # budget units charged per adaptation
    robust: bool = True                               # #914: run the K-002/K-003 robustness pass after refine
    quarantine_min_net: int = 0                       # K-002: drop bullets with net_utility BELOW this (0 = net-negative)


def adapt_once(trajectory: Trajectory, playbook: Playbook, *, chat: Callable[[str], str],
               embed: "Optional[Callable[[List[str]], List[List[float]]]]" = None,
               budget=None, config: "Optional[AdaptConfig]" = None) -> dict:
    """One online adaptation step over *playbook* (mutated in place — cumulative, O-002). Budget-gated: if a
    *budget* ledger is given and cannot afford ``config.cost``, the step is skipped (no LLM call). Otherwise:
    reflect (label-free, L-001 rounds) → curate → apply_delta → refine, then charge the budget. FAIL-SOFT:
    an empty reflection (transport/parse error) yields a no-op result; never raises. Returns a summary."""
    config = config or AdaptConfig()
    base = {"skipped": False, "reflected": 0, "added": 0, "rated": 0,
            "merged": 0, "pruned": 0, "charged": 0, "rounds_run": 0, "resolved": 0, "quarantined": 0}
    if budget is not None:
        try:
            if not budget.can_afford(config.cost):
                base["skipped"] = True
                return base
        except Exception:  # noqa: BLE001 — a flaky ledger must not break the loop; treat as unaffordable
            base["skipped"] = True
            return base
    used = [b for b in playbook.bullets() if b.id in set(trajectory.used_bullet_ids)]
    ro = reflect(trajectory, chat=chat, used_bullets=used, rounds=config.rounds)
    base["rounds_run"] = ro.rounds_run
    if ro.is_empty():
        return base                                   # nothing learned; no charge, no mutation
    delta = curate(ro)
    summary = apply_delta(delta, playbook)
    ref = refine(playbook, embed=embed, dedup_threshold=config.dedup_threshold,
                 max_bullets=config.max_bullets, max_chars=config.max_chars, lazy=config.refine_lazy)
    # #914: the paper's grow-and-refine ROBUSTNESS half — run the self-correction pass after refine so it is
    # part of the live online loop (was orphaned). resolve_contradictions (K-003: keep the higher-utility of a
    # contradicted same-section pair) + quarantine_noisy (K-002: drop net-negative bullets a noisy reflector
    # produced). Off-hot-path (adapt runs on the ReflectionWorker); fail-soft; gated by config.robust.
    resolved = quarantined = 0
    if getattr(config, "robust", True):
        try:
            from .robust import resolve_contradictions, quarantine_noisy   # sibling; no import cycle
            resolved = resolve_contradictions(playbook).get("resolved", 0)
            quarantined = quarantine_noisy(playbook, min_net=getattr(config, "quarantine_min_net", 0)).get("removed", 0)
        except Exception:  # noqa: BLE001 — the robustness pass is advisory; never break the adapt step
            pass
    if budget is not None:
        try:
            budget.charge(config.cost)
            base["charged"] = config.cost
        except Exception:  # noqa: BLE001 — never break the loop on a charge error
            pass
    base.update(reflected=len(ro.insights), added=summary["added"], rated=summary["rated"],
                merged=ref["merged"], pruned=ref["pruned"], resolved=resolved, quarantined=quarantined)
    return base


class OnlineAdapter:
    """Binds a playbook + the injected transports + config so a trajectory can be adapted with one call.
    The unit the background worker drives."""

    def __init__(self, playbook: Playbook, *, chat: Callable[[str], str],
                 embed: "Optional[Callable[[List[str]], List[List[float]]]]" = None,
                 budget=None, config: "Optional[AdaptConfig]" = None):
        self.playbook = playbook
        self.chat = chat
        self.embed = embed
        self.budget = budget
        self.config = config or AdaptConfig()

    def adapt(self, trajectory: Trajectory) -> dict:
        return adapt_once(trajectory, self.playbook, chat=self.chat, embed=self.embed,
                          budget=self.budget, config=self.config)


class ReflectionWorker:
    """The async seam between the hot path and the LLM reflection work (C0 correctness requirement). The
    engine's `post_feedback` hook calls :meth:`submit` — an O(1), non-blocking enqueue that NEVER runs the
    model inline; a background daemon (started via :meth:`start`) drains the queue and runs *process* off the
    turn path. Fail-soft: a bad item increments ``errors`` but never kills the worker; a full queue drops the
    item (``dropped``) rather than block the hot path."""

    def __init__(self, process: "Callable[[Trajectory], object]", *, max_queue: int = 1000):
        self._q: "queue.Queue" = queue.Queue(maxsize=max_queue)
        self._process = process
        self._thread: "Optional[threading.Thread]" = None
        self._stop = threading.Event()
        self.processed = 0
        self.errors = 0
        self.dropped = 0

    def submit(self, item: Trajectory) -> bool:
        """Non-blocking enqueue (hot-path-safe). Returns False + counts a drop if the queue is full."""
        try:
            self._q.put_nowait(item)
            return True
        except queue.Full:
            self.dropped += 1
            return False

    def pending(self) -> int:
        return self._q.qsize()

    def _run_one(self, item: Trajectory) -> None:
        try:
            self._process(item)
            self.processed += 1
        except Exception:  # noqa: BLE001 — a failing reflection must not kill the worker
            self.errors += 1

    def process_pending(self) -> int:
        """Drain the queue synchronously in the caller's thread (deterministic — used by tests + a 'lazy'
        no-background-thread mode). Returns the number processed."""
        n = 0
        while True:
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                break
            self._run_one(item)
            n += 1
        return n

    def start(self) -> None:
        """Start the background daemon that drains the queue off the hot path (idempotent)."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()

        def _loop() -> None:
            while not self._stop.is_set():
                try:
                    item = self._q.get(timeout=0.25)
                except queue.Empty:
                    continue
                self._run_one(item)

        self._thread = threading.Thread(target=_loop, name="ace-reflection-worker", daemon=True)
        self._thread.start()

    def _drain_pending(self) -> None:
        """Discard queued items not yet started, narrowing stop()'s in-flight window to a single item."""
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                break

    def stop(self, timeout: "Optional[float]" = None) -> None:
        """Stop the daemon and wait for it to exit.

        Queued reflections that have not started are discarded, so at most the single in-flight item can
        finish. The default join waits to completion. If a finite timeout expires, the live thread remains
        observable and :meth:`start` cannot spawn an overlapping worker.
        """
        self._stop.set()
        t = self._thread
        if t is None:
            return
        self._drain_pending()
        t.join(timeout=timeout)
        if not t.is_alive():
            self._thread = None
