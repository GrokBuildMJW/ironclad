"""Provider routing-policy engine (P0) — pure, deterministic, spawn-free.

Maps one reasoning item (a RouteRequest) to a provider from the registry along three axes, in this
fail-closed order: (1) sovereignty filter (SENSITIVE / local-only → local providers only, else decline
— never a silent offload), (2) capability filter (web/file/effort ceiling), (3) load/spill (Spark is
the default target unless the chat-turn lock is held or the batch width is saturated → spill to
external CLIs), (4) cost/effort scoring, (5) budget gate, (6) deterministic tie-break.

No subprocess, no network, no ironclad import — just schema + arithmetic, so the decisions are
snapshot-testable and the sovereignty guarantee is assertable (§9). The effort→max_tokens table is
the single source feeding both the cost estimate and (downstream) the Spark governor — never a
separate MPR token notion.
"""
from __future__ import annotations

from enum import Enum
from typing import Dict, List, Optional, Set

from pydantic import BaseModel, Field

from providers import ProviderRegistry, ProviderSpec


class ProviderPolicy(str, Enum):
    LOCAL_ONLY = "local-only"      # NEVER external (sovereignty) — must land on capabilities.local
    OFFLOADABLE = "offloadable"    # may be offloaded


class Sensitivity(str, Enum):
    PUBLIC = "public"              # public research → offload allowed
    INTERNAL = "internal"         # internal context → prefer local
    SENSITIVE = "sensitive"       # private code/secrets → forces local-only


class RouteRequest(BaseModel):
    index: int                                    # input position (order-preserving, 1:1 to items[])
    effort: str = "medium"                        # low|medium|high|xhigh (→ max_tokens, §4.1)
    provider_policy: ProviderPolicy = ProviderPolicy.OFFLOADABLE
    sensitivity: Sensitivity = Sensitivity.INTERNAL
    needs_web: bool = False                       # needs web_search capability
    needs_file_io: bool = False                   # needs file_io capability
    est_input_chars: int = 0                      # len(item)+len(context), filled by the dispatcher
    excluded_provider_ids: List[str] = Field(default_factory=list)   # SOFT distinct-reviewer anti-affinity
                                                  # (#457, FORK-F): provider(s) to avoid (e.g. the agent that
                                                  # PRODUCED the artifact under review) so a review-of-a-review
                                                  # is not routed back to its author — waived if no equal peer
                                                  # remains. Caller-passed; route_one stays pure (§ router purity).


class LoadSignal(BaseModel):
    """Live load for the spill axis. Filled by the server, fail-soft (defaults = idle)."""
    spark_inflight: int = 0                       # currently running Spark reasoning calls (+already-routed-local)
    spark_batch_width: int = 8                    # effective batch width (= _WORKERS.max_concurrency)
    spark_chat_busy: bool = False                 # a /chat turn holds the agent lock → keep Spark free


class Budget(BaseModel):
    """Cost/rate cap per run (caller supplies; router counts along, §4.3)."""
    usd_cap: Optional[float] = None               # hard $ ceiling for the run (None = no limit)
    per_provider_max_concurrent: Dict[str, int] = Field(default_factory=dict)


class RouteDecision(BaseModel):
    index: int
    provider_id: Optional[str]                    # None ⇒ not routable (caller fail-soft)
    reason: str                                   # local-sovereign | spill-load | spill-budget | cost-fit |
                                                  # no-local-provider | no-capable-provider | budget-exhausted
    est_max_tokens: int                           # §4.1 mapping, passed to the substrate
    est_cost_usd: float                           # for budget counting + manifest
    distinct_reviewer: Optional[str] = None       # #457 anti-affinity provenance: "applied" (an excluded
                                                  # producer was dropped and an equal peer chosen), "waived"
                                                  # (the producer was the ONLY capable agent → SOFT-kept), or
                                                  # None (no exclusion requested). Snapshot-stable.


# ── Scoring SSOT (module constants). NOTE: providers.scoring is a RESERVED config seam — it exists in the
#    config tree but the router does NOT yet read it; it applies these fixed built-ins (see config-runtime.md,
#    "treat it as reserved until it is wired"). SCORING-1 (#503). ────────────────────────────────────────
EFFORT_RANK = {"low": 0, "medium": 1, "high": 2, "xhigh": 3}
EFFORT_MAX_TOKENS = {"low": 512, "medium": 1024, "high": 2048, "xhigh": 4096}
W_COST = 1.0
W_SENSITIVITY = 0.5
COST_NORM_USD = 0.10
INPUT_CHARS_PER_TOKEN = 4


def _effort(req_effort: str) -> str:
    return req_effort if req_effort in EFFORT_RANK else "medium"


def _est_input_tokens(input_chars: int) -> int:
    return max(0, input_chars) // INPUT_CHARS_PER_TOKEN


def _cost_usd(p: ProviderSpec, in_tok: int, out_tok: int) -> float:
    return p.cost_per_1k_in * in_tok / 1000.0 + p.cost_per_1k_out * out_tok / 1000.0


def _effort_fit(p: ProviderSpec, effort: str) -> float:
    """In [0,1]: 1.0 when max_effort exactly fits; falls 0.25 per tier of surplus/deficit."""
    gap = abs(EFFORT_RANK[p.capabilities.max_effort] - EFFORT_RANK[effort])
    return max(0.0, 1.0 - 0.25 * gap)


def _cost_penalty(p: ProviderSpec, in_tok: int, out_tok: int) -> float:
    return W_COST * (_cost_usd(p, in_tok, out_tok) / COST_NORM_USD)   # local (cost_*=0) ⇒ 0.0


def _sensitivity_penalty(p: ProviderSpec, sensitivity: Sensitivity) -> float:
    return W_SENSITIVITY if (sensitivity == Sensitivity.INTERNAL and not p.capabilities.local) else 0.0


def _score(p: ProviderSpec, effort: str, in_tok: int, out_tok: int, sensitivity: Sensitivity) -> float:
    return (_effort_fit(p, effort)
            - _cost_penalty(p, in_tok, out_tok)
            - _sensitivity_penalty(p, sensitivity)
            + p.weight / 1000.0)


def _pick(pool: List[ProviderSpec], effort: str, in_tok: int, out_tok: int, sensitivity: Sensitivity) -> ProviderSpec:
    """Deterministic: score desc, then weight desc, then provider_id asc (snapshot-stable)."""
    return sorted(
        pool,
        key=lambda p: (-_score(p, effort, in_tok, out_tok, sensitivity), -p.weight, p.provider_id),
    )[0]


def route_one(
    req: RouteRequest,
    registry: ProviderRegistry,
    load: LoadSignal,
    budget: Budget,
    spent: float = 0.0,
    effort_max_tokens: Optional[Dict[str, int]] = None,
) -> RouteDecision:
    effort = _effort(req.effort)
    # The per-effort output-token cap comes from ``providers.effort_max_tokens`` (threaded from the
    # dispatcher) when configured, else the module default. A malformed table, a missing key, or a
    # non-positive-int value falls back to the module default so route_one stays pure and never raises.
    emt = effort_max_tokens if isinstance(effort_max_tokens, dict) else EFFORT_MAX_TOKENS
    cap = emt.get(effort, EFFORT_MAX_TOKENS[effort])
    out_tok = cap if isinstance(cap, int) and not isinstance(cap, bool) and cap > 0 else EFFORT_MAX_TOKENS[effort]
    in_tok = _est_input_tokens(req.est_input_chars)
    distinct: Optional[str] = None    # #457 anti-affinity provenance (set after the capability filter)

    def decision(p: Optional[ProviderSpec], reason: str) -> RouteDecision:
        cost = round(_cost_usd(p, in_tok, out_tok), 6) if p is not None else 0.0
        return RouteDecision(
            index=req.index,
            provider_id=p.provider_id if p is not None else None,
            reason=reason,
            est_max_tokens=out_tok,
            est_cost_usd=cost,
            distinct_reviewer=distinct,
        )

    candidates = list(registry.by_id().values())

    # 1) Sovereignty filter (hard, first — fail-closed against leak)
    force_local = req.sensitivity == Sensitivity.SENSITIVE or req.provider_policy == ProviderPolicy.LOCAL_ONLY
    if force_local:
        candidates = [p for p in candidates if p.capabilities.local]
        if not candidates:
            return decision(None, "no-local-provider")

    # 2) Capability filter (web / file_io / effort ceiling — ordinal, not lexicographic)
    def cap_ok(p: ProviderSpec) -> bool:
        if req.needs_web and not p.capabilities.web_search:
            return False
        if req.needs_file_io and not p.capabilities.file_io:
            return False
        return EFFORT_RANK[p.capabilities.max_effort] >= EFFORT_RANK[effort]

    candidates = [p for p in candidates if cap_ok(p)]
    if not candidates:
        return decision(None, "no-capable-provider")

    # 2b) SOFT distinct-reviewer anti-affinity (#457, FORK-F): drop the excluded producer(s) so a
    #     review is never routed back to its author — but ONLY while ≥1 capable peer remains (the rule
    #     is conditional on an equal peer being available). If excluding them would empty the capable
    #     set, WAIVE and record provenance (never decline a route over a SOFT preference). Applied here,
    #     before load/spill/budget, so the reduced set propagates through every later axis (the producer
    #     cannot slip back in via a spill/budget fallback). HARD axes (sovereignty/budget) still outrank it.
    if req.excluded_provider_ids:
        excl: Set[str] = set(req.excluded_provider_ids)
        kept = [p for p in candidates if p.provider_id not in excl]
        if len(kept) == len(candidates):
            pass                         # the producer wasn't among the candidates → no-op exclusion:
                                         # distinct stays None (don't claim "applied" for a route nothing changed)
        elif kept:
            candidates, distinct = kept, "applied"   # producer dropped, ≥1 equal peer remains
        else:
            distinct = "waived"          # the producer was the ONLY capable agent → SOFT-kept (never declined)
    # NOTE (HARD outranks SOFT): a later HARD axis (sovereignty already applied above; the budget gate
    # below) may still decline or re-pin. The intended distinct-reviewer producers are EXTERNAL CLI agents,
    # never the local provider, so the cost-0 local stays an affordable fallback and the exclusion does not
    # cause a budget decline in practice; HARD budget/sovereignty deliberately win over this SOFT preference.

    locals_ = [p for p in candidates if p.capabilities.local]
    externals = [p for p in candidates if not p.capabilities.local]

    # 3) Load/spill: local (Spark) is the default target unless the chat lock is held or the batch
    #    width is saturated (spark_inflight already includes items routed local earlier this run).
    spark_full = load.spark_inflight >= load.spark_batch_width
    spill = (load.spark_chat_busy or spark_full) and bool(externals)

    if force_local:
        pool, base_reason = locals_, "local-sovereign"
    elif spill:
        pool, base_reason = externals, "spill-load"
    else:
        # idle: local is the default target (§3.2-3); only spill scores among externals.
        pool = locals_ if locals_ else externals
        base_reason = "local-sovereign" if locals_ else "cost-fit"

    # 5) Budget gate (applied before scoring so cost-exhausted candidates drop out)
    if budget.usd_cap is not None:
        affordable = [p for p in pool if spent + _cost_usd(p, in_tok, out_tok) <= budget.usd_cap]
        if not affordable:
            cheap_local = [p for p in locals_ if spent + _cost_usd(p, in_tok, out_tok) <= budget.usd_cap]
            if cheap_local:
                return decision(_pick(cheap_local, effort, in_tok, out_tok, req.sensitivity), "spill-budget")
            return decision(None, "budget-exhausted")
        pool = affordable

    # 4)+6) Cost/effort scoring + deterministic tie-break
    best = _pick(pool, effort, in_tok, out_tok, req.sensitivity)
    if force_local or best.capabilities.local:
        reason = "local-sovereign"
    elif base_reason == "spill-load":
        reason = "spill-load"
    else:
        reason = "cost-fit"
    return decision(best, reason)
