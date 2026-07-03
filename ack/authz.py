"""#1071: per-principal identity + authorization (RBAC) + multi-tenant memory namespacing.

Ironclad's trust model is single-tenant today (open / token / sealed — ONE token for the whole server, no
per-principal identity, no tenant isolation). This is the PURE foundation for multi-user / enterprise: a
``Principal`` (id / role / tenant), a role → permitted-danger-tier RBAC policy, principal resolution from a
token map, and tenant-scoped memory namespacing. Pure / stdlib-only; the engine gates it
(``security.multi_tenant``, default OFF) and wires it.

The FULL request-path authorization (resolve the principal on every request, enforce per tool call),
attribute-based (ABAC) rules, the memory-service tenant enforcement, and per-tenant model routing are
**explicit remaining scope** — see ADR-0014; overlaps the Enterprise milestone (#20).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, Optional

# Danger tiers — mirror the command-spec's classification (read_only / mutating / destructive / costly).
READ_ONLY, MUTATING, DESTRUCTIVE, COSTLY = "read_only", "mutating", "destructive", "costly"
_ALL_TIERS = (READ_ONLY, MUTATING, DESTRUCTIVE, COSTLY)

#: Built-in RBAC policy: role → the danger tiers it may perform. **Deny-by-default** (an unknown role ⇒ {}).
DEFAULT_ROLES: "Dict[str, FrozenSet[str]]" = {
    "admin":    frozenset(_ALL_TIERS),
    "operator": frozenset((READ_ONLY, MUTATING, COSTLY)),   # everything but destructive
    "agent":    frozenset((READ_ONLY, MUTATING, COSTLY)),   # the autonomous agent — no destructive ops
    "reader":   frozenset((READ_ONLY,)),
}


@dataclass(frozen=True)
class Principal:
    """WHO is acting: an *id*, a *role* (keys the RBAC policy), and a *tenant* (keys memory/data isolation)."""

    id: str = "anonymous"
    role: str = "operator"       # single-tenant default = the operator (backward-compatible with today's model)
    tenant: str = "default"


#: The default principal for the single-tenant model (multi_tenant OFF) — the operator on the local box.
ANONYMOUS = Principal()


def authorize(role: str, tier: str, *, roles: "Optional[Dict[str, FrozenSet[str]]]" = None) -> bool:
    """RBAC decision: may a principal of *role* perform an action of danger *tier*? **Deny-by-default** (an
    unknown role or tier ⇒ False). Pure; never raises."""
    policy = roles if roles is not None else DEFAULT_ROLES
    try:
        return str(tier) in policy.get(str(role), frozenset())
    except Exception:   # noqa: BLE001
        return False


def resolve_principal(token: str, principals: "Optional[Dict[str, Any]]") -> "Principal":
    """Map a bearer *token* to a :class:`Principal` via a ``{token: {id, role, tenant}}`` map. The operator
    wires the map; the token VALUES come from the deployment env, never hardcoded (secret-free). An
    unknown/empty token ⇒ :data:`ANONYMOUS`. Pure; never raises."""
    try:
        rec = (principals or {}).get(token or "")
        if not isinstance(rec, dict):
            return ANONYMOUS
        return Principal(id=str(rec.get("id", "anonymous")), role=str(rec.get("role", "reader")),
                         tenant=str(rec.get("tenant", "default")))
    except Exception:   # noqa: BLE001
        return ANONYMOUS


def tenant_scope(scope: str, tenant: str) -> str:
    """Namespace a memory scope by *tenant* so multi-tenant memory is isolated — ``<tenant>::<scope>``. The
    ``default`` tenant (and an empty one) is a NO-OP → byte-identical to the single-tenant partition. Pure."""
    t = (tenant or "").strip()
    if not t or t == "default":
        return scope
    return f"{t}::{scope}"
