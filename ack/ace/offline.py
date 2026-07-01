"""ACE offline adaptation — batch context optimization over a dataset, multi-epoch, with a parallel
deterministic delta merge and a test-split evaluation.

Epic #855 cluster ACE-ADAPT-OFFLINE (catalogue A-003 configurable batch size, C-003 parallel merge,
G-001 offline adaptation + test-split pass@1, G-003 multi-epoch, G-004 offline warmup for the online loop).

Where `online.adapt_once` (#862) handles ONE trajectory on the hot-path-adjacent worker, this is the
OFFLINE counterpart: an operator-invoked batch build over a whole dataset (or the execution ledger),
off any turn path. It composes the same per-cluster pieces:

    samples ──(per batch)── reflect → curate → [Delta, Delta, …] ──merge_deltas──▶ apply_delta → refine
                                                                    (deterministic, parallel-safe)

`build_offline` runs ``max_epochs`` (default 5, G-003) over batches of ``batch_size`` (default 1, A-003);
each batch's per-sample deltas are computed **independently** (an injected ``map_fn`` may parallelize the
reflect calls) and **merged deterministically** (`merge_deltas`, C-003 — input-order, last-wins ADD
collision). `evaluate` is the test-split **pass@1** (G-001) — it needs only the optimized playbook, never
the training data. `warmup` is the label-free batch-replay of the operator's execution ledger (G-004) that
seeds the playbook the online loop then continues on.

LABEL-FREE (O-001): the training labels are used ONLY in `evaluate`; `build_offline` adapts from execution
outcome alone. The chat / embed / budget transports are INJECTED (like `online`), so this module stays
pure / stdlib-only. FAIL-SOFT throughout — a bad sample is skipped, never raises.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Optional

from .playbook import Playbook
from .reflector import Trajectory, reflect
from .curator import Delta, curate, apply_delta, OP_ADD
from .grow import refine, DEFAULT_DEDUP_THRESHOLD
from .generator import prepare_context, to_trajectory, DEFAULT_TOP_K


@dataclass
class OfflineConfig:
    """Offline-build hyperparameters. The base config (`batch_size=1`) matches the online step (A-003); the
    paper's multi-epoch default is 5 (G-003)."""

    batch_size: int = 1                               # A-003 (base = 1)
    max_epochs: int = 5                               # G-003 (default 5)
    rounds: int = 1                                   # reflection rounds per sample (L-001)
    top_k: int = DEFAULT_TOP_K                        # bullets injected into a sample's context
    dedup_threshold: float = DEFAULT_DEDUP_THRESHOLD
    max_bullets: Optional[int] = None
    max_chars: Optional[int] = None
    cost: int = 1                                     # budget units charged per productive batch


@dataclass
class Sample:
    """One offline dataset item: a `query`, an optional `expected` answer (used ONLY by `evaluate`, never by
    the label-free build), and an optional pre-built `trajectory` (replay mode — the warmup/ledger path)."""

    query: str
    expected: Optional[str] = None
    trajectory: Optional[Trajectory] = None


def merge_deltas(deltas: "List[Optional[Delta]]") -> Delta:
    """Combine per-batch deltas into ONE deterministically (C-003). Ops are kept in input order; **ADD**
    ops collide on ``(section, content)`` and the **last wins** (its position is preserved); RATE / TAG /
    REMOVE ops accumulate (each is an idempotent-ish counter/tag/delete the merge keeps). The result applies
    identically no matter how the inputs were computed (so the per-sample deltas may be produced in parallel)."""
    ops: list = []
    add_at: dict = {}                                 # (section, content) → index in `ops` (last-wins)
    reasons: List[str] = []
    for d in deltas:
        if d is None:
            continue
        if getattr(d, "reasoning", ""):
            reasons.append(d.reasoning)
        for op in d.operations:
            if op.op == OP_ADD:
                key = (op.section, op.content.strip())
                if key in add_at:
                    ops[add_at[key]] = op             # last-wins collision resolution
                else:
                    add_at[key] = len(ops)
                    ops.append(op)
            else:
                ops.append(op)
    return Delta(reasoning=" | ".join(reasons), operations=ops)


def _trajectory_for(sample: Sample, playbook: Playbook, *, chat: Callable[[str], str],
                    embed, run, top_k: int) -> "Optional[Trajectory]":
    """A sample's trajectory: replay its pre-built one (warmup/ledger), else run the injected model over the
    relevant playbook context (label-free — the outcome is execution feedback, NOT the expected label)."""
    if sample.trajectory is not None:
        return sample.trajectory                      # replay mode (G-004)
    if run is None:
        return None
    ctx = prepare_context(playbook, sample.query, embed=embed, top_k=top_k)
    out = run(sample.query, ctx.text)
    if isinstance(out, Trajectory):
        return out
    return to_trajectory(sample.query, steps=[str(out)], outcome="completed", context=ctx)


def _sample_delta(sample: Sample, playbook: Playbook, *, chat, embed, run, config: OfflineConfig):
    """Reflect → curate ONE sample into a Delta (or None). Independent per sample → parallel-safe. Fail-soft."""
    try:
        traj = _trajectory_for(sample, playbook, chat=chat, embed=embed, run=run, top_k=config.top_k)
        if traj is None:
            return None
        used = [b for b in playbook.bullets() if b.id in set(traj.used_bullet_ids)]
        ro = reflect(traj, chat=chat, used_bullets=used, rounds=config.rounds)
        if ro.is_empty():
            return None
        return curate(ro)
    except Exception:  # noqa: BLE001 — a bad sample is skipped, never breaks the build
        return None


def build_offline(samples, playbook: Playbook, *, chat: Callable[[str], str],
                  embed=None, budget=None, config: "Optional[OfflineConfig]" = None,
                  run: "Optional[Callable[[str, str], object]]" = None, map_fn=map) -> dict:
    """Offline adaptation over *samples* into *playbook* (mutated in place). For ``max_epochs`` (G-003), in
    batches of ``batch_size`` (A-003), build a Trajectory per sample (replay `.trajectory`, or
    ``run(query, context)`` over the relevant playbook subset), reflect→curate each into a per-sample Delta —
    computed **independently** so ``map_fn`` (default sequential `map`) may parallelize them — then
    `merge_deltas` deterministically (C-003), apply, and refine. Budget-gated per productive batch (stops when
    unaffordable). LABEL-FREE (the labels are for `evaluate` only). Fail-soft. Returns a build report."""
    config = config or OfflineConfig()
    report = {"epochs_run": 0, "batches": 0, "samples_seen": 0,
              "added": 0, "rated": 0, "merged": 0, "pruned": 0, "skipped": 0, "charged": 0}
    items = [s if isinstance(s, Sample) else Sample(query=str(s)) for s in (samples or [])]
    if not items:
        return report
    bs = max(1, int(config.batch_size))
    epochs = max(1, int(config.max_epochs))
    for epoch in range(epochs):                       # G-003: revisit the same samples each epoch
        report["epochs_run"] = epoch + 1
        for i in range(0, len(items), bs):
            batch = items[i:i + bs]
            report["batches"] += 1
            report["samples_seen"] += len(batch)
            if budget is not None:                    # gate BEFORE the (expensive) reflect calls
                try:
                    if not budget.can_afford(config.cost):
                        return report
                except Exception:  # noqa: BLE001 — a flaky ledger stops the build, never raises
                    return report
            deltas = list(map_fn(lambda s: _sample_delta(s, playbook, chat=chat, embed=embed,
                                                          run=run, config=config), batch))
            combined = merge_deltas(deltas)
            if combined.is_empty():
                report["skipped"] += len(batch)
                continue
            summary = apply_delta(combined, playbook)
            report["added"] += summary["added"]
            report["rated"] += summary["rated"]
            ref = refine(playbook, embed=embed, dedup_threshold=config.dedup_threshold,
                         max_bullets=config.max_bullets, max_chars=config.max_chars, lazy=True)
            report["merged"] += ref["merged"]
            report["pruned"] += ref["pruned"]
            if budget is not None:
                try:
                    budget.charge(config.cost)
                    report["charged"] += config.cost
                except Exception:  # noqa: BLE001 — never break the build on a charge error
                    pass
    return report


def evaluate(playbook: Playbook, samples, *, run: "Callable[[str, str], object]",
             score: "Optional[Callable[[object, str], bool]]" = None, embed=None,
             top_k: int = DEFAULT_TOP_K) -> dict:
    """Test-split **pass@1** evaluation (G-001): for each sample with an `expected`, run the injected model
    once over the playbook context for its query and score the output (default: exact string match). Uses ONLY
    the optimized *playbook* — the training data need NOT be available at inference time. Returns
    ``{n, passed, accuracy}``. Fail-soft (a sample that errors counts as not-passed)."""
    judge = score or (lambda out, exp: str(out).strip() == str(exp).strip())
    n = 0
    passed = 0
    for s in samples or ():
        sample = s if isinstance(s, Sample) else Sample(query=str(s))
        if sample.expected is None:
            continue
        n += 1
        try:
            ctx = prepare_context(playbook, sample.query, embed=embed, top_k=top_k)
            out = run(sample.query, ctx.text)
            if isinstance(out, Trajectory):
                out = out.steps[-1] if out.steps else out.outcome
            if judge(out, sample.expected):
                passed += 1
        except Exception:  # noqa: BLE001 — a failed run is a miss, never breaks the eval
            pass
    return {"n": n, "passed": passed, "accuracy": (passed / n if n else 0.0)}


def warmup(ledger, playbook: Playbook, *, chat: Callable[[str], str], embed=None,
           budget=None, config: "Optional[OfflineConfig]" = None) -> dict:
    """Offline warmup (G-004): label-free batch-replay of the operator's execution **ledger** (a sequence of
    past `Trajectory`s) to seed *playbook*, which the online loop then continues on (hand the SAME playbook to
    `OnlineAdapter`). A thin `build_offline` in replay mode — no `run`/labels. Returns the build report."""
    samples = [s if isinstance(s, Sample)
               else Sample(query=getattr(s, "query", ""), trajectory=s) if isinstance(s, Trajectory)
               else Sample(query=str(s))
               for s in (ledger or [])]
    return build_offline(samples, playbook, chat=chat, embed=embed, budget=budget, config=config)
