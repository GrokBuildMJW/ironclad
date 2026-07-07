"""MPR umbrella config + run budget (Spec 09 §1/§2/§3/§6.3/§8).

``MprConfig`` is the one config object run()/1f consumes. It COMPOSES the existing sub-loaders
(``RouterConfig`` from config.py, ``RegistryConfig`` from registry/config.py) under the ``mpr.*`` section
— no duplication — and adds the sovereignty / budget / provider-pool config. Defaults equal the §2.1
code-default block; secret-free (no Spark-IP/host/vessel literals — endpoints come from connection.*,
secrets only from ``*_api_key_env`` ENV names). ``RunBudget`` is the per-run cost/token accumulator
(complements P0's BudgetLedger). The global precedence (defaults < file < env < cli) is ironclad's;
``_apply_mpr_env`` layers the ``GX10_MPR_*`` env overrides on the ``mpr`` section.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from .config import RouterConfig, load_router_config
from .registry.config import RegistryConfig, load_registry_config

# ── §2.1 default provider pool / routing (generic, secret-free placeholders) ──────────────────────
DEFAULT_POOL: Dict[str, dict] = {
    "spark-vllm": {"kind": "in-engine", "policy_class": "local-only", "connection_ref": "connection",
                   "cost_per_1k_in_usd": 0.0, "cost_per_1k_out_usd": 0.0,
                   "effort_levels": ["low", "medium", "high"]},
    "claude-sonnet": {"kind": "code-cli", "policy_class": "offloadable", "agent": "SONNET",
                      "model": "claude-sonnet-5", "cost_per_1k_in_usd": 0.003,
                      "cost_per_1k_out_usd": 0.015, "effort_levels": ["low", "medium", "high"]},
    "claude-opus": {"kind": "code-cli", "policy_class": "offloadable", "agent": "OPUS",
                    "model": "claude-opus-4-8", "cost_per_1k_in_usd": 0.015,
                    "cost_per_1k_out_usd": 0.075, "effort_levels": ["high", "xhigh"]},
    "kimi": {"kind": "code-cli", "policy_class": "offloadable", "agent": "KIMI", "model": "kimi-k2",
             "cost_per_1k_in_usd": 0.0006, "cost_per_1k_out_usd": 0.0025,
             "effort_levels": ["medium", "high"]},
}
DEFAULT_ROUTING: Dict[str, Any] = {
    "spill_when_spark_busy": True,
    "effort_to_provider": {"low": "spark-vllm", "medium": "claude-sonnet",
                           "high": "claude-sonnet", "xhigh": "claude-opus"},
}


# ── config models (§2.1) ──────────────────────────────────────────────────────────────────────────
class SovereigntyCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    default_policy: str = "offloadable"
    internal_is_local_only: bool = True
    fail_closed: bool = True


class BudgetCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_cost_usd_per_run: float = 2.00
    max_tokens_per_run: int = 200000
    per_provider: Dict[str, dict] = Field(default_factory=dict)
    on_exceed: str = "degrade"               # degrade | truncate | abort


class RoutingCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    spill_when_spark_busy: bool = True
    effort_to_provider: Dict[str, str] = Field(default_factory=dict)


class ProvidersCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    default_offload: str = "claude-sonnet"
    pool: Dict[str, dict] = Field(default_factory=lambda: dict(DEFAULT_POOL))
    routing: RoutingCfg = Field(default_factory=lambda: RoutingCfg(**DEFAULT_ROUTING))


class MprConfig(BaseModel):
    """The umbrella MPR config (§2.1). ``extra=ignore`` so the router/registry sub-keys that live in the
    same ``mpr`` section (read by their own loaders) don't trip validation."""

    model_config = ConfigDict(extra="ignore")
    enabled: bool = True                     # runtime active-gate (default ON; mpr.enabled off pauses live)
    audit_level: str = "full-per-perspective"
    runs_dir: str = "runs/mpr"
    # in-engine panel execution mode (the two switchable best paths; non-"deep" → "direct"):
    #  "direct" (DEFAULT, stable): thinking-off → perspectives write their analysis directly to the token
    #           budget (no <think> starvation), full fan-out concurrency, fast.
    #  "deep": thinking-on + per-effort token budgets → deeper reasoning, the governor throttles concurrency.
    panel_mode: str = "direct"
    sovereignty: SovereigntyCfg = Field(default_factory=SovereigntyCfg)
    budget: BudgetCfg = Field(default_factory=BudgetCfg)
    providers: ProvidersCfg = Field(default_factory=lambda: ProvidersCfg(
        pool=dict(DEFAULT_POOL), routing=RoutingCfg(**DEFAULT_ROUTING)))
    router: RouterConfig = Field(default_factory=RouterConfig)
    # MPR-REG-1 (#503): RESERVED config seam — the registry sub-config (mpr.registry.* : roles_min/max,
    # effort_max_tokens, distinct_max_overlap, adaptive_min_roles, panels_dir) is LOADED + validated here but
    # the panel resolver/guards/loader do NOT yet read it (they apply the module constants in
    # registry/resolve.py + the Panel model). Treat these knobs as reserved until they are wired; setting
    # them has no effect today (operator decision, #503: mark reserved honestly rather than fully wire/remove).
    registry: RegistryConfig = Field(default_factory=RegistryConfig)


def load_mpr_config(cfg_tree: Optional[dict]) -> MprConfig:
    """Build the umbrella config from the merged config tree's ``mpr`` section, composing the existing
    router + registry sub-loaders (precedence-agnostic; the tree is already merged by ironclad)."""
    mpr = (cfg_tree or {}).get("mpr") or {}
    top = {k: mpr[k] for k in ("enabled", "audit_level", "runs_dir", "panel_mode", "sovereignty",
                               "budget", "providers") if k in mpr}
    router = load_router_config(mpr.get("router"))
    registry = load_registry_config(mpr)
    return MprConfig(**top, router=router, registry=registry)


def resolve_provider_pool(cfg_tree: Optional[dict]) -> dict:
    """Promotion-path indirection (§1): ``dispatch.providers`` wins when present (P0 core promotion),
    else ``mpr.providers``. Returns the pool dict (or the default pool)."""
    cfg_tree = cfg_tree or {}
    disp = (cfg_tree.get("dispatch") or {}).get("providers")
    if disp:
        return disp.get("pool", disp) if isinstance(disp, dict) else {}
    mpr_prov = (cfg_tree.get("mpr") or {}).get("providers") or {}
    return mpr_prov.get("pool", dict(DEFAULT_POOL))


def _truthy(v: Any) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _apply_mpr_env(cfg_tree: dict, env: Optional[dict] = None) -> dict:
    """Layer ``GX10_MPR_*`` env overrides onto the ``mpr`` section (§2.3) — in the plugin, not a core
    edit. Nested sections (sovereignty/budget) handled explicitly. Mutates + returns *cfg_tree*."""
    env = env if env is not None else os.environ
    mpr = cfg_tree.setdefault("mpr", {})
    # NOTE: MPR is a core built-in (always loaded, no GX10_MPR load gate — ADR-0002 #115). mpr.enabled
    # is the RUNTIME active-gate (default ON), toggled in-session via `/config set mpr.enabled on|off`.
    # GX10_MPR_ENABLED is the optional deploy-time override of that runtime default (below).
    if "GX10_MPR_ENABLED" in env:
        mpr["enabled"] = _truthy(env["GX10_MPR_ENABLED"])   # optional deploy-time runtime default
    if "GX10_MPR_AUDIT_LEVEL" in env:
        mpr["audit_level"] = env["GX10_MPR_AUDIT_LEVEL"]
    if "GX10_MPR_RUNS_DIR" in env:
        mpr["runs_dir"] = env["GX10_MPR_RUNS_DIR"]
    if "GX10_MPR_PANEL_MODE" in env:
        mpr["panel_mode"] = env["GX10_MPR_PANEL_MODE"]
    sov = mpr.setdefault("sovereignty", {})
    if "GX10_MPR_DEFAULT_POLICY" in env:
        sov["default_policy"] = env["GX10_MPR_DEFAULT_POLICY"]
    if "GX10_MPR_FAIL_CLOSED" in env:
        sov["fail_closed"] = _truthy(env["GX10_MPR_FAIL_CLOSED"])
    bud = mpr.setdefault("budget", {})
    if "GX10_MPR_MAX_COST_USD" in env:
        bud["max_cost_usd_per_run"] = float(env["GX10_MPR_MAX_COST_USD"])
    if "GX10_MPR_MAX_TOKENS" in env:
        bud["max_tokens_per_run"] = int(env["GX10_MPR_MAX_TOKENS"])
    if "GX10_MPR_ON_EXCEED" in env:
        bud["on_exceed"] = env["GX10_MPR_ON_EXCEED"]
    if "GX10_MPR_DEFAULT_OFFLOAD" in env:
        mpr.setdefault("providers", {})["default_offload"] = env["GX10_MPR_DEFAULT_OFFLOAD"]
    return cfg_tree


# ── §6.3 RunBudget (per-run cost/token accumulator) ───────────────────────────────────────────────
@dataclass
class RunBudget:
    max_cost_usd: float
    max_tokens: int
    per_provider: Dict[str, dict] = field(default_factory=dict)
    on_exceed: str = "degrade"
    _spent_cost: Dict[str, float] = field(default_factory=dict)
    _spent_tok: Dict[str, int] = field(default_factory=dict)

    def can_admit(self, provider: str, est_cost: float, est_tok: int) -> bool:
        """True if charging (est_cost, est_tok) to *provider* stays within BOTH the run cap and the
        per-provider cap (the tighter one wins — both are checked)."""
        if sum(self._spent_cost.values()) + est_cost > self.max_cost_usd:
            return False
        if sum(self._spent_tok.values()) + est_tok > self.max_tokens:
            return False
        pp = self.per_provider.get(provider)
        if pp:
            cap_c = pp.get("max_cost_usd_per_run")
            cap_t = pp.get("max_tokens_per_run")
            if cap_c is not None and self._spent_cost.get(provider, 0.0) + est_cost > cap_c:
                return False
            if cap_t is not None and self._spent_tok.get(provider, 0) + est_tok > cap_t:
                return False
        return True

    def charge(self, provider: str, cost: float, tok: int) -> None:
        self._spent_cost[provider] = self._spent_cost.get(provider, 0.0) + cost
        self._spent_tok[provider] = self._spent_tok.get(provider, 0) + tok

    def remaining(self) -> dict:
        return {"cost_usd": self.max_cost_usd - sum(self._spent_cost.values()),
                "tokens": self.max_tokens - sum(self._spent_tok.values())}

    def spent(self) -> dict:
        return {"cost_usd": round(sum(self._spent_cost.values()), 6),
                "tokens": sum(self._spent_tok.values()),
                "per_provider": {p: {"cost_usd": round(self._spent_cost.get(p, 0.0), 6),
                                     "tokens": self._spent_tok.get(p, 0)}
                                 for p in set(self._spent_cost) | set(self._spent_tok)}}


def cheaper_provider(provider: str, pool: Dict[str, dict]) -> Optional[str]:
    """The next-cheaper offloadable provider (highest out-cost still below *provider*'s) for §6.3
    degrade. None if none is cheaper."""
    cur = (pool.get(provider) or {}).get("cost_per_1k_out_usd", 0.0)
    cheaper = [(pid, (s or {}).get("cost_per_1k_out_usd", 0.0)) for pid, s in pool.items()
               if (s or {}).get("policy_class") == "offloadable" and pid != provider
               and (s or {}).get("cost_per_1k_out_usd", 0.0) < cur]
    return sorted(cheaper, key=lambda c: c[1])[-1][0] if cheaper else None
