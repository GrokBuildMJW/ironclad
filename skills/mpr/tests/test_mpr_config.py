"""MPR umbrella config + RunBudget + manifest §7 fields (Spec 09 §1/2/3/6.3/7 / §9.1,§9.4).

Config: defaults (enabled off A/B-gate), env overrides incl. nested, provider-pool indirection, no
private literals. Budget: run/per-provider caps (tighter wins), no charge for local-only, degrade picks
cheaper. Manifest carries the optional security/budget/sovereignty_summary blocks.
"""
from __future__ import annotations

import re

from mpr.audit import BudgetSummary, Manifest, Provenance, Query, RouterDecisionSnapshot, SecurityBlock, SovereigntySummary
from mpr.mpr_config import (
    DEFAULT_POOL,
    MprConfig,
    RunBudget,
    _apply_mpr_env,
    cheaper_provider,
    load_mpr_config,
    resolve_provider_pool,
)


# ── §9.1 config ───────────────────────────────────────────────────────────────────────────────────
def test_default_block_present_and_disabled():
    cfg = load_mpr_config(None)
    assert cfg.enabled is False                         # A/B-gate off by default
    assert cfg.audit_level == "full-per-perspective" and cfg.runs_dir == "runs/mpr"
    assert cfg.sovereignty.fail_closed is True and cfg.sovereignty.default_policy == "offloadable"
    assert "spark-vllm" in cfg.providers.pool and cfg.providers.default_offload == "claude-sonnet"
    assert cfg.providers.pool["spark-vllm"]["policy_class"] == "local-only"


def test_panel_mode_default_override_and_env():
    assert load_mpr_config(None).panel_mode == "direct"                      # stable default
    assert load_mpr_config({"mpr": {"panel_mode": "deep"}}).panel_mode == "deep"
    tree = {}
    _apply_mpr_env(tree, env={"GX10_MPR_PANEL_MODE": "deep"})
    assert load_mpr_config(tree).panel_mode == "deep"                        # env override


def test_composes_router_and_registry_subconfigs():
    cfg = load_mpr_config({"mpr": {"router": {"min_panel": 4}, "roles": {"max": 6}}})
    assert cfg.router.min_panel == 4          # RouterConfig sub-loader honored
    assert cfg.registry.roles_max == 6        # RegistryConfig sub-loader honored


def test_env_override_enables_and_nested():
    cfg_tree = {}
    _apply_mpr_env(cfg_tree, env={"GX10_MPR_ENABLED": "1", "GX10_MPR_AUDIT_LEVEL": "manifest-only",
                                  "GX10_MPR_FAIL_CLOSED": "0", "GX10_MPR_MAX_COST_USD": "0.5"})
    cfg = load_mpr_config(cfg_tree)
    assert cfg.enabled is True and cfg.audit_level == "manifest-only"   # GX10_MPR_ENABLED = runtime active-gate
    assert cfg.sovereignty.fail_closed is False             # nested env override
    assert cfg.budget.max_cost_usd_per_run == 0.5           # nested env override


def test_gx10_mpr_is_load_gate_not_runtime_enable():
    # GX10_MPR registers the tool (LOAD gate, read by mpr_enabled) — it must NOT flip the runtime
    # mpr.enabled flag (the in-session /config set toggle, default off). Decoupled load vs runtime.
    tree = {}
    _apply_mpr_env(tree, env={"GX10_MPR": "1"})
    assert load_mpr_config(tree).enabled is False


def test_provider_pool_no_private_literals():
    # boundary spirit: no Spark-IP / host literal in the default pool.
    blob = str(DEFAULT_POOL)
    assert not re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", blob)   # no IPv4
    assert "192.168" not in blob and ".local" not in blob


def test_resolve_provider_pool_indirection():
    # dispatch.providers (P0 promotion) wins over mpr.providers.
    promoted = {"dispatch": {"providers": {"pool": {"x": {"policy_class": "local-only"}}}}}
    assert "x" in resolve_provider_pool(promoted)
    assert "spark-vllm" in resolve_provider_pool({"mpr": {"providers": {"pool": DEFAULT_POOL}}})
    assert "spark-vllm" in resolve_provider_pool(None)      # default pool fallback


# ── §9.4 budget ────────────────────────────────────────────────────────────────────────────────────
def _budget(**over):
    d = {"max_cost_usd": 2.0, "max_tokens": 200000,
         "per_provider": {"claude-opus": {"max_cost_usd_per_run": 0.10, "max_tokens_per_run": 5000}}}
    d.update(over)
    return RunBudget(**d)


def test_budget_run_cap_blocks():
    b = _budget(max_cost_usd=1.0)
    assert b.can_admit("claude-sonnet", 0.9, 1000) is True
    b.charge("claude-sonnet", 0.9, 1000)
    assert b.can_admit("claude-sonnet", 0.2, 1000) is False   # 0.9+0.2 > 1.0


def test_per_provider_cap_tighter_wins():
    b = _budget()
    # run cap is huge, but opus per-provider cap is 0.10 → 0.2 blocked.
    assert b.can_admit("claude-opus", 0.2, 1000) is False
    assert b.can_admit("claude-sonnet", 0.2, 1000) is True    # sonnet has no per-provider cap


def test_no_charge_for_local_only():
    b = _budget()
    b.charge("spark-vllm", 0.0, 5000)                          # local cost 0
    assert b.spent()["cost_usd"] == 0.0                        # local-only never raises cost


def test_budget_degrade_picks_cheaper_provider():
    assert cheaper_provider("claude-opus", DEFAULT_POOL) == "claude-sonnet"   # 0.075 → 0.015
    assert cheaper_provider("kimi", DEFAULT_POOL) is None     # already cheapest offloadable


# ── §7 manifest fields ─────────────────────────────────────────────────────────────────────────────
def test_manifest_carries_security_budget_sovereignty_blocks():
    m = Manifest(
        run_id="r", created_at="2026-06-19T00:00:00Z", query=Query(text="q"),
        router_decision=RouterDecisionSnapshot(decision="run"), provenance=Provenance(sovereignty_ok=True),
        security=SecurityBlock(profile="sealed", code_locality="local", permission_mode_effective="plan"),
        budget=BudgetSummary(max_cost_usd_per_run=2.0, spent_cost_usd=0.41),
        sovereignty_summary=SovereigntySummary(local_only_count=3, offloaded_count=2, violations=0),
    )
    d = m.model_dump()
    assert d["security"]["permission_mode_effective"] == "plan"
    assert d["budget"]["spent_cost_usd"] == 0.41
    assert d["sovereignty_summary"]["violations"] == 0
    assert Manifest.model_validate_json(m.model_dump_json()) == m   # still lossless with new blocks


def test_manifest_blocks_optional_default_none():
    m = Manifest(run_id="r", created_at="t", query=Query(text="q"),
                 router_decision=RouterDecisionSnapshot(decision="run"),
                 provenance=Provenance(sovereignty_ok=True))
    assert m.security is None and m.budget is None and m.sovereignty_summary is None
