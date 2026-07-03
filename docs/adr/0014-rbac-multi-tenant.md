# ADR-0014: Per-principal identity + RBAC + multi-tenant memory namespacing

## Status
Accepted — foundational increment (epic #1065 / #1071). **Default-off.** Overlaps the Enterprise milestone
(#20 / #29-31).

## Context
Ironclad's trust model is single-tenant (open / token / sealed — ONE token for the whole server). There is no
per-principal identity, no role-based authorization, and no tenant isolation of the accumulated memory — a
prerequisite for multi-user / enterprise deployment.

## Decision
A core authz foundation (`ack.authz`), pure + default-off:
- **`Principal`** (id / role / tenant) — who is acting.
- **`DEFAULT_ROLES`** — a **deny-by-default** RBAC policy mapping a role to the danger tiers it may perform
  (`read_only` / `mutating` / `destructive` / `costly`): `admin`=all, `operator`/`agent`=all-but-destructive,
  `reader`=read_only.
- **`authorize(role, tier)`** — the RBAC decision (deny-by-default).
- **`resolve_principal(token, principals)`** — a bearer token → Principal via an operator-wired map (token
  VALUES from env, **secret-free**); unknown ⇒ `ANONYMOUS`.
- **`tenant_scope(scope, tenant)`** — namespace a memory scope by tenant (`<tenant>::<scope>`) so multi-tenant
  memory is isolated; the `default` tenant is a NO-OP (byte-identical single-tenant partition).
- The engine gates it (`security.multi_tenant`, default off) and exposes `_authorize_action` /
  `_tenant_mem_scope` as the wiring seam.

## Consequences
- **Default-off (byte-identical):** single-tenant deployments are unchanged.
- The RBAC helper **fails OPEN** on a wiring error — the foundation is not yet the sole gate, so a bug must
  not lock out the operator.

## Remaining scope (explicit — NOT faked here)
- **Full request-path authorization:** resolve the principal on every request + enforce per tool call / command.
- Attribute-based (ABAC) rules beyond role→tier (resource owner, time, source).
- **Memory-service tenant enforcement:** the service currently trusts the caller's `agent_id`; a tenant
  boundary must be enforced server-side, not merely scoped by the caller.
- Per-tenant model routing + quotas.
- A principal/token administration surface. This overlaps the Enterprise milestone (#20 / #29-31).
