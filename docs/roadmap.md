# Roadmap

> Honest and explicit: this separates **what works today** from **what is planned**.
> For the per-component wiring status of shipped features, see
> [`status.md`](status.md). Treat `main` as a development snapshot.

## Where Ironclad is today (single-tenant, home-LAN trust)

Ironclad currently runs as a **single-operator** system:

- **One principal.** There is exactly one identity — the operator running the
  deployment. The TaskStore, the memory namespace, and every tool run in that one
  context. There is **no multi-user authentication or authorization** yet.
- **Home-LAN trust transport.** The orchestrator server is reached like the model
  port: plain client-initiated HTTP on a trusted network, **no auth**. Code locality
  is structural — the client pulls handovers and runs the code-agents on its own
  machine; project code never has to leave it.

This is a deliberate, honest starting point that matches a **sovereign / local
deployment**: your box, your model, your data, one operator. It is *not* yet safe to
expose to untrusted networks or to share between users.

## In progress — Phase d: secure, session-gated channel (still single-tenant)

Hardening the PC↔server channel for a single operator, **without** pretending to be
multi-user:

- **Selectable trust profiles** (`security.profile`): `open` (today's behaviour —
  out-of-the-box, no auth, mount allowed), `token` (shared deployment secret over the
  LAN), `sealed` (localhost-bind behind a client-managed tunnel + deployment secret +
  session heartbeat).
- **Explicit session lifecycle.** The client opens a session, heartbeats it, and
  closes it on exit; when no live session exists the server **seals** — client-facing
  endpoints refuse, and background planning pauses.
- **The token is a *deployment secret*, not a user login.** It proves "this is my
  client process," nothing more. Because there is still exactly one principal, this
  adds no per-user scoping and makes no multi-tenant promise.
- **Code-locality as policy, not a hard ban.** `open`/`token` allow a code mount if
  you want it; `sealed` enforces pull-only, code-stays-local.

## Planned — Phase g: Identity & Authorization (multi-tenant)

Real multi-user, enterprise/government deployments need far more than a token, and
**none of it exists yet** — listing it honestly rather than implying it ships:

- A **principal/scope** threaded through the whole engine: TaskStore ownership,
  per-principal **memory namespaces** (so one user's memory never bleeds into
  another's), and **entitlement-scoped data sources**.
- **Organisation structures and groups** — role/attribute-based access (RBAC/ABAC)
  driven by identity-provider claims (OIDC / SAML).
- Audit trails and per-tenant isolation guarantees.

Until Phase g lands, treat any "enterprise/government" use as **single-tenant on
trusted infrastructure**. Multi-tenant identity is a direction, not a feature.

## Also planned

- Broaden test coverage; harden the new server/client paths.
- Verified connection recipes for more locally-served open models.
- Retrieval / **RAG over local datasets** through the memory hook.
- First tagged release once the APIs settle.

Issues and discussions are welcome — this is an early, openly-developed project.
