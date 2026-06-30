"""Pure reflection-trigger policy for the memory-service (stdlib-only, offline-testable like scope_guard).

The central, threshold-triggered reflection counts learning writes; once ``REFLECT_EVERY`` accumulate it fires
a background graph-hygiene run (one at a time, guarded by a non-blocking lock).

MEMSVC-1 (#503 / #767): the old design reset the counter only at the END of a run. So every write that landed
*during* a run (graph hygiene over Neo4j can take a while) re-triggered the threshold, spawned a daemon that
immediately bailed on the busy lock (thread churn), and was then zeroed when the run finished (undercount).

This module isolates the fire DECISION as a pure function so it is unit-testable without mem0/neo4j/fastapi:
the counter is CONSUMED at fire time and a fire is SUPPRESSED while a reflection is already running, so writes
during a run keep accumulating toward the NEXT cycle (no undercount) and no bail-thread is spawned (no churn).
"""
from __future__ import annotations

from typing import Tuple


def reflect_decision(writes_since: int, every: int, reflection_running: bool) -> "Tuple[int, bool]":
    """Account one learning write and decide whether to fire a reflection.

    Returns ``(new_writes_since, fire)``:
      * always increments the counter by one;
      * fires (``True``) **and consumes** the counter back to ``0`` **iff** the threshold is reached AND no
        reflection is currently running;
      * otherwise returns the incremented counter with ``fire=False`` — so writes that arrive while a run is in
        progress keep accumulating toward the next cycle (no undercount), and the threshold path never spawns a
        thread just to bail on the busy lock (no churn).

    Never raises: a non-int ``writes_since`` is treated as 0, and a non-int / non-positive ``every`` disables
    firing (the counter simply accumulates).
    """
    try:
        n = int(writes_since) + 1
    except (TypeError, ValueError, OverflowError):
        n = 1
    try:
        threshold = int(every)
    except (TypeError, ValueError, OverflowError):
        threshold = 0
    if threshold >= 1 and n >= threshold and not reflection_running:
        return 0, True
    return n, False
