"""Provider dispatch (P0) — per-substrate governor + budget ledger (this file grows the full
ProviderDispatcher in the next step; here are the pure, spawn-free primitives §4 needs).

Two substrates, each under its OWN governor (never a shared one), so the Spark envelope stays exactly
today's:
  * Spark (in-engine): the envelope lives UNCHANGED in ``ReasoningWorkers._plan_concurrency`` and is
    reached via ``fanout(...)`` — P0 does not reimplement it (ride, don't duplicate).
  * PC-pool (cli): ``plan_pool_concurrency`` caps the ThreadPool at min(max_agents, provider cap, n).

The effort→max_tokens table is the single SSOT in ``router.EFFORT_MAX_TOKENS`` (re-exported here for
callers that plan the pool); it feeds both the cost estimate and the Spark governor — never a separate
MPR token notion. ``BudgetLedger`` carries the running spend (§4.3): charge the estimate before
dispatch, reconcile with the real completion-token cost afterwards.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Semaphore
from typing import Any, Callable, Dict, List, Optional, Sequence

from providers import ProviderKind, ProviderRegistry, ProviderSpec
from router import (  # single SSOT (§4.1) — re-exported, not redefined
    Budget,
    EFFORT_MAX_TOKENS,
    INPUT_CHARS_PER_TOKEN,
    LoadSignal,
    ProviderPolicy,
    RouteDecision,
    RouteRequest,
    Sensitivity,
    route_one,
)
from workers import ReasoningWorkers

__all__ = [
    "EFFORT_MAX_TOKENS", "plan_pool_concurrency", "BudgetLedger",
    "DispatchPolicy", "DispatchResult", "ProviderDispatcher", "PROVENANCE_FIELDS",
]

#: Audit contract (§8): the provenance keys every ACTIVE-path DispatchResult carries, which the MPR
#: run-manifest records per perspective — so local-only/sensitive items are provably local (no leak).
#: P0 supplies these fields; manifest writing + TaskStore indexing live in the MPR plugin (spec 02 §10).
PROVENANCE_FIELDS = (
    "provider_id", "provider_kind", "model", "effort",
    "est_cost_usd", "real_cost_usd", "route_reason", "spilled",
    "latency", "completion_tokens",
)


def plan_pool_concurrency(n: int, max_agents: int, provider_max_concurrent: int) -> int:
    """CLI-pool governor: in-flight CLI agents for one provider = min(client cap, provider cap, n).

    Mirrors the Spark envelope's spirit for the pool lane; always ≥ 1 (a single item still runs).
    ``max_agents`` is the client cap (DEFAULT_MAX_AGENTS / --max-agents, client.py:83);
    ``provider_max_concurrent`` is the provider's ``rate_limit.max_concurrent``. Effective = the min.
    """
    return max(1, min(int(max_agents), int(provider_max_concurrent), int(n)))


class BudgetLedger:
    """Running per-run spend for the budget gate (§4.3). fail-soft, never raises.

    Flow: the router's budget gate reads ``spent`` to drop unaffordable candidates; the dispatcher
    ``charge``s the estimate before dispatch, then ``reconcile``s with the real cost afterwards.
    """

    def __init__(self, spent: float = 0.0) -> None:
        self.spent: float = max(0.0, float(spent))

    def can_afford(self, cost: float, cap: Optional[float]) -> bool:
        if cap is None:
            return True
        return self.spent + max(0.0, float(cost)) <= float(cap)

    def charge(self, cost: float) -> float:
        self.spent += max(0.0, float(cost))
        return self.spent

    def reconcile(self, estimate: float, actual: float) -> float:
        """Replace a charged estimate with the real cost (actual − estimate), clamped at ≥ 0."""
        self.spent = max(0.0, self.spent + (max(0.0, float(actual)) - max(0.0, float(estimate))))
        return self.spent


class DispatchPolicy:
    """Bundles the router inputs for one run."""

    def __init__(self, requests: Sequence[RouteRequest], *, system: Optional[str] = None,
                 load: Optional[LoadSignal] = None, budget: Optional[Budget] = None,
                 allow_spill: bool = True) -> None:
        self.requests: List[RouteRequest] = list(requests)
        self.system = system
        self.load = load
        self.budget = budget
        self.allow_spill = allow_spill


class DispatchResult(dict):
    """A workers result dict (ok/content/error/completion_tokens/latency) + routing provenance
    (provider_id/provider_kind/model/effort/est_cost_usd/real_cost_usd/route_reason/spilled) —
    additive keys only, same shape as today's fan-out result."""


def _real_cost(spec: ProviderSpec, est_in_tok: int, completion_tokens: Optional[int], fallback: float) -> float:
    if not isinstance(completion_tokens, (int, float)) or isinstance(completion_tokens, bool):
        return fallback   # defensive: a non-numeric completion_tokens must never raise out of dispatch()
    return round(spec.cost_per_1k_in * est_in_tok / 1000.0
                 + spec.cost_per_1k_out * float(completion_tokens) / 1000.0, 6)


class ProviderDispatcher:
    """Provider-agnostic dispatch OVER the existing substrates. Brings no own fan-out/store:
    in-engine → ReasoningWorkers.fanout, local setup.type → an injected agent_runner (a local
    co-located subprocess CLI). Inactive (no pool / disabled) ⇒ delegates the whole batch to fanout,
    byte-identical to today's parallel_reason."""

    def __init__(self, registry: Optional[ProviderRegistry], *, workers: Optional[ReasoningWorkers],
                 agent_runner: Optional[Callable[..., Dict[str, Any]]] = None,
                 effort_max_tokens: Optional[Dict[str, int]] = None,
                 enabled: bool = False, max_agents: int = 3) -> None:
        self._registry = registry
        self._workers = workers
        self._runner = agent_runner
        self._emt = dict(effort_max_tokens or EFFORT_MAX_TOKENS)
        self._enabled = bool(enabled)
        self._max_agents = max(1, int(max_agents))

    def active(self) -> bool:
        return self._enabled and self._registry is not None and bool(self._registry.providers)

    def dispatch(self, items: Sequence[str], contexts: Optional[Sequence[Optional[str]]] = None,
                 policy: Optional[DispatchPolicy] = None, *, max_tokens: Optional[int] = None,
                 temperature: float = 0.7, think: bool = True) -> List[DispatchResult]:
        items = list(items)
        n = len(items)
        if n == 0:
            return []
        ctxs: List[Optional[str]] = list(contexts) if contexts is not None else [None] * n
        if len(ctxs) != n:
            ctxs = [None] * n
        system = policy.system if policy else None

        # 4) inactive → byte-identical whole-batch fanout (additive-key-free passthrough)
        if not self.active():
            if self._workers is None:
                return [self._unroutable(i, "no-substrate") for i in range(n)]
            return [DispatchResult(r) for r in self._workers.fanout(
                items, system=system, contexts=ctxs, max_tokens=max_tokens,
                temperature=temperature, think=think)]

        by_id = self._registry.by_id()
        reqs = self._requests(policy, items, ctxs, n)
        load = policy.load.model_copy() if (policy and policy.load) else LoadSignal()
        budget = policy.budget if (policy and policy.budget) else Budget()
        allow_spill = policy.allow_spill if policy else True
        # P0 routes the whole batch up front, so the budget gate (route_one) reads the running
        # ESTIMATE via ledger.spent; the real per-item cost is surfaced as result["real_cost_usd"]
        # for the manifest (§8). ledger.reconcile(est, real) is the utility P0-7 uses to aggregate
        # the run's real spend — not fed back into this run's already-completed routing.
        ledger = BudgetLedger()

        decisions: List[RouteDecision] = []
        for req in reqs:
            d = route_one(req, self._registry, load, budget, ledger.spent)
            decisions.append(d)
            if d.provider_id is not None:
                ledger.charge(d.est_cost_usd)
                if by_id[d.provider_id].capabilities.local:
                    load.spark_inflight += 1   # local routed → fills the envelope for the next item

        # Partition by capabilities.local (NOT by kind) — the only local substrate is the Spark
        # fanout; everything external goes to the CLI runner. A non-local in-engine provider has no
        # remote runner in P0 → unsupported (fail-soft, no provenance lie). Consistent with the
        # spark_inflight/spill axes which also key on capabilities.local.
        results: List[Optional[DispatchResult]] = [None] * n
        spark: List[RouteDecision] = []
        cli: List[RouteDecision] = []
        for d in decisions:
            if d.provider_id is None:
                results[d.index] = self._unroutable(d.index, d.reason)
                continue
            spec = by_id[d.provider_id]
            if spec.capabilities.local:
                spark.append(d)
            elif spec.kind == ProviderKind.CLI:
                cli.append(d)
            else:
                results[d.index] = self._unroutable(d.index, "unsupported-substrate")

        if spark:
            self._run_spark(spark, items, ctxs, reqs, by_id, system, temperature, think, results)
        if cli:
            self._run_cli(cli, items, reqs, by_id, results)

        if allow_spill:
            self._spill_failed_to_local(results, decisions, reqs, items, ctxs, by_id, system, temperature, think)

        return [r if r is not None else self._unroutable(i, "no-result") for i, r in enumerate(results)]

    # ── helpers ──────────────────────────────────────────────────────────────────────────────────
    def _requests(self, policy, items, ctxs, n) -> List[RouteRequest]:
        base = policy.requests if (policy and policy.requests and len(policy.requests) == n) else None
        out: List[RouteRequest] = []
        for i in range(n):
            chars = len(items[i]) + len(ctxs[i] or "")
            if base is not None:
                out.append(base[i].model_copy(update={"index": i, "est_input_chars": chars}))
            else:
                out.append(RouteRequest(index=i, est_input_chars=chars))
        return out

    def _attach(self, base, spec, decision, req, *, spilled, reason=None) -> DispatchResult:
        in_tok = max(0, req.est_input_chars) // INPUT_CHARS_PER_TOKEN  # same SSOT as the router's estimate
        est = 0.0 if spilled else decision.est_cost_usd
        r = DispatchResult(base)
        r.update({
            "provider_id": spec.provider_id, "provider_kind": spec.kind.value, "model": spec.model,
            "effort": req.effort, "est_cost_usd": est,
            "real_cost_usd": _real_cost(spec, in_tok, base.get("completion_tokens"), est),
            "route_reason": reason or decision.reason, "spilled": spilled,
        })
        return r

    def _run_spark(self, spark, items, ctxs, reqs, by_id, system, temperature, think, results) -> None:
        if self._workers is None:                      # local routed but no Spark substrate → fail-soft
            for d in spark:
                results[d.index] = self._unroutable(d.index, "no-spark-substrate")
            return
        idxs = [d.index for d in spark]
        fres = self._workers.fanout([items[i] for i in idxs], system=system,
                                    contexts=[ctxs[i] for i in idxs],
                                    max_tokens=max(d.est_max_tokens for d in spark),
                                    temperature=temperature, think=think)
        for d, r in zip(spark, fres):
            results[d.index] = self._attach(r, by_id[d.provider_id], d, reqs[d.index], spilled=False)

    def _run_cli(self, cli, items, reqs, by_id, results) -> None:
        if self._runner is None:                       # no client lane injected → fail-soft, no spawn
            for d in cli:
                results[d.index] = self._unroutable(d.index, "no-cli-runner")
            return
        sems: Dict[str, Semaphore] = {}
        for d in cli:
            pid = d.provider_id
            if pid not in sems:
                cap = plan_pool_concurrency(len(cli), self._max_agents, by_id[pid].rate_limit.max_concurrent)
                sems[pid] = Semaphore(cap)

        def _do(d):
            spec = by_id[d.provider_id]
            try:                                       # isolate EVERYTHING (runner + _attach) — pool.map re-raises
                with sems[spec.provider_id]:
                    base = self._runner(spec, items[d.index], effort=reqs[d.index].effort, max_tokens=d.est_max_tokens)
                return d.index, self._attach(base, spec, d, reqs[d.index], spilled=False)
            except Exception as e:  # noqa: BLE001 — one bad CLI call ≠ batch failure, never throws out of dispatch()
                return d.index, self._unroutable(d.index, repr(e))

        with ThreadPoolExecutor(max_workers=self._max_agents, thread_name_prefix="cli") as pool:
            for idx, res in pool.map(_do, cli):
                results[idx] = res

    def _spill_failed_to_local(self, results, decisions, reqs, items, ctxs, by_id, system, temperature, think) -> None:
        if self._workers is None:
            return
        local_spec = next((p for p in self._registry.by_id().values() if p.capabilities.local), None)
        retry = []
        for d in decisions:
            r, req = results[d.index], reqs[d.index]
            if r is None or r.get("ok") or d.provider_id is None:
                continue   # unroutable (provider_id None) stays unroutable — §5.3-3, no capability-blind spill
            spillable = req.provider_policy != ProviderPolicy.LOCAL_ONLY and req.sensitivity != Sensitivity.SENSITIVE
            already_local = by_id[d.provider_id].capabilities.local
            if spillable and not already_local:
                retry.append(d)
        if not retry:
            return
        fres = self._workers.fanout([items[d.index] for d in retry], system=system,
                                    contexts=[ctxs[d.index] for d in retry],
                                    max_tokens=max(d.est_max_tokens for d in retry),
                                    temperature=temperature, think=think)
        for d, r in zip(retry, fres):
            if not r.get("ok"):
                continue                                # retry also failed → keep the original ok=False
            if local_spec is not None:
                results[d.index] = self._attach(r, local_spec, d, reqs[d.index], spilled=True, reason="spill-fallback")
            else:
                rr = DispatchResult(r)
                rr.update({"provider_id": "spark-fallback", "provider_kind": "in-engine", "model": "",
                           "effort": reqs[d.index].effort, "est_cost_usd": 0.0, "real_cost_usd": 0.0,
                           "route_reason": "spill-fallback", "spilled": True})
                results[d.index] = rr

    def _unroutable(self, index, reason) -> DispatchResult:
        return DispatchResult({
            "ok": False, "content": None, "error": f"unroutable: {reason}",
            "completion_tokens": None, "latency": 0.0,
            "provider_id": None, "provider_kind": None, "model": None, "effort": None,
            "est_cost_usd": 0.0, "real_cost_usd": 0.0, "route_reason": reason, "spilled": False,
        })
