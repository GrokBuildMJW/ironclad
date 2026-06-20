"""Sovereignty + security enforcement engine (Spec 09 §4 + §5 + §6 chokepoint).

The core guarantee: ``local-only`` data NEVER goes to an external provider — resolved deterministically
(fail-closed), enforced at a single chokepoint BEFORE any dispatch, provable in the manifest.

Reiten, nicht duplizieren: this is the RUN-TIME layer over the per-role policy the registry already
resolved (``registry.resolve.resolve_policy(panel, role)`` → ``perspective.provider_policy``). Here we
only (a) UPGRADE toward local-only on internal/mixed evidence or repo-context (never downgrade), (b)
pick a substrate, (c) hard-guard the choice, (d) downgrade the code-CLI permission to read-only, (e)
clamp the effort to the provider's accepted levels. Execution itself is delegated to P0 (run()/1f).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

_LOCAL_POLICY = "local-only"
_OFFLOAD_POLICY = "offloadable"

# §4.2 permission totally-ordered (restrictive = smaller = fewer mutation rights).
_PERM_RANK = {"plan": 0, "default": 1, "acceptEdits": 2, "bypassPermissions": 3}


class SovereigntyViolation(RuntimeError):
    """A local-only perspective would be dispatched to an external provider — fail-closed, pre-dispatch."""


@dataclass(frozen=True)
class ProviderChoice:
    provider: str
    policy: str            # local-only | offloadable
    permission: str        # rendered code-CLI permission (read-only for offload)
    effort: str            # effort clamped to the provider's accepted levels


# ── §4.2 permission-mode (downgrade-only) ─────────────────────────────────────────────────────────
def min_restrictive(a: str, b: str) -> str:
    """Return the more restrictive of two permission modes; unknown/empty → most restrictive ('plan').

    Always returns a VALID mode: if the winner is itself unknown (both inputs unknown), fall to 'plan'
    so no caller can ever render a bogus CLI flag (defensive — exported API).
    """
    ra, rb = _PERM_RANK.get(a, 0), _PERM_RANK.get(b, 0)
    res = a if ra <= rb else b
    return res if res in _PERM_RANK else "plan"


def effective_permission(operator_default: str) -> str:
    """MPR offload perspectives are read-only reasoning → render 'plan', never looser than the operator
    default (downgrade-only, §4.2). Unknown/empty operator default → 'plan' (fail-closed) — the result
    is always a VALID permission mode (never the raw unknown string, which would be a bad CLI flag)."""
    od = operator_default if operator_default in _PERM_RANK else "plan"
    return min_restrictive(od, "plan")


# ── §5.1 sovereignty resolution (deterministic, fail-closed, upgrade-only) ────────────────────────
def resolve_sovereignty(*, role_policy: Optional[str], evidence_source: Optional[str],
                        reads_repo_context: bool = False, default_policy: str = _OFFLOAD_POLICY,
                        internal_is_local_only: bool = True, fail_closed: bool = True) -> str:
    """Resolve a perspective's run-time policy. Upgrades toward local-only only — a local-only role can
    never become offloadable (monotonic). Ambiguous → local-only when fail_closed (no guessing)."""
    base = role_policy or default_policy
    if internal_is_local_only and evidence_source in ("internal", "mixed"):
        base = _LOCAL_POLICY
    if reads_repo_context:
        base = _LOCAL_POLICY
    if base not in (_LOCAL_POLICY, _OFFLOAD_POLICY):
        base = _LOCAL_POLICY if fail_closed else _OFFLOAD_POLICY
    return base


# ── §6.1 provider choice + §6.2 effort clamp ──────────────────────────────────────────────────────
def local_provider(pool: Dict[str, Any]) -> str:
    """The (first) provider whose policy_class is local-only — the only valid target for local-only."""
    for pid, spec in pool.items():
        if (spec or {}).get("policy_class") == _LOCAL_POLICY:
            return pid
    return "spark-vllm"


def clamp_effort(effort: str, provider_effort_levels: List[str]) -> str:
    """Clamp the CLI {effort} to the provider's accepted levels (§6.2 M3): if unsupported, fall to the
    highest accepted level so no offload silently dies on an invalid flag. Empty levels → effort as-is."""
    if not provider_effort_levels or effort in provider_effort_levels:
        return effort
    order = ["low", "medium", "high", "xhigh"]
    accepted = [lv for lv in order if lv in provider_effort_levels]
    return accepted[-1] if accepted else effort


def choose_provider(*, policy: str, effort: str, pool: Dict[str, Any], routing: Dict[str, Any],
                    default_offload: str, spark_busy: bool = False) -> str:
    """Pick a provider for a perspective (§6.1). local-only → the local provider only; offloadable spills
    to a CLI when Spark is busy, else light effort may stay local."""
    local = local_provider(pool)
    if policy == _LOCAL_POLICY:
        return local
    e2p = (routing or {}).get("effort_to_provider", {}) or {}
    if (routing or {}).get("spill_when_spark_busy") and spark_busy:
        return e2p.get(effort) or default_offload
    if effort == "low":
        return local                                   # Spark free enough → keep light work local
    return e2p.get(effort, default_offload)


def assert_sovereign(role: str, policy: str, chosen: str, pool: Dict[str, Any]) -> None:
    """The single hard guard (§5.2): a local-only policy may only land on a local-only provider. Raised
    BEFORE any net/CLI call, so local-only data physically never leaves the box."""
    pc = (pool.get(chosen) or {}).get("policy_class")
    if policy == _LOCAL_POLICY and pc != _LOCAL_POLICY:
        raise SovereigntyViolation(
            f"perspective {role!r} is local-only but provider {chosen!r} is external (policy_class={pc!r})"
        )


# ── §5.2 single chokepoint: resolve → choose → guard → permission/effort ──────────────────────────
def plan_perspective_dispatch(*, role: str, role_policy: Optional[str], effort: str,
                              evidence_source: Optional[str], reads_repo_context: bool,
                              pool: Dict[str, Any], routing: Dict[str, Any], default_offload: str,
                              operator_permission: str, spark_busy: bool = False,
                              default_policy: str = _OFFLOAD_POLICY,
                              internal_is_local_only: bool = True,
                              fail_closed: bool = True) -> ProviderChoice:
    """The ONE place a perspective is routed to a substrate. Resolves the policy, chooses a provider,
    HARD-guards sovereignty (raises SovereigntyViolation on a leak), renders the read-only permission,
    and clamps the effort. Execution is delegated downstream to P0 (run()/1f); this never calls out."""
    policy = resolve_sovereignty(
        role_policy=role_policy, evidence_source=evidence_source,
        reads_repo_context=reads_repo_context, default_policy=default_policy,
        internal_is_local_only=internal_is_local_only, fail_closed=fail_closed,
    )
    chosen = choose_provider(policy=policy, effort=effort, pool=pool, routing=routing,
                             default_offload=default_offload, spark_busy=spark_busy)
    assert_sovereign(role, policy, chosen, pool)       # fail-closed, pre-dispatch
    permission = effective_permission(operator_permission)
    eff = clamp_effort(effort, (pool.get(chosen) or {}).get("effort_levels", []))
    return ProviderChoice(provider=chosen, policy=policy, permission=permission, effort=eff)
