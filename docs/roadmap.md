# Roadmap

> Forward-looking only: this is what is **planned or in progress** — none of it
> ships yet. For what already works today, see [`status.md`](status.md); for how the
> docs are organised, see [`docs-guide.md`](docs-guide.md). Items below are directions,
> not commitments, and may change.
>
> **Generated** from the open roadmap phases (open milestones) —
> do not edit by hand; a phase drops off automatically once its milestone is closed.

Ironclad runs today as a sovereign, single-operator system — one principal,
home-LAN trust, code stays on your machine. The work below extends it without
giving up the core promise: self-hosted, model-agnostic, no vendor lock-in.

## Enterprise & multi-tenant readiness

The classic requirements for organisational and enterprise/government use —
**everything auth-, identity- and governance-related lives here**. None of it
exists yet: today there is exactly one principal, and the channel token is a
*deployment secret*, not a user login.

- **Identity & authorization (multi-tenant).** A principal/scope threaded
  through the whole engine: per-principal TaskStore ownership, isolated
  **memory namespaces** (one tenant's memory never bleeds into another's), and
  **entitlement-scoped data sources**.
- **Enterprise identity integration.** RBAC/ABAC driven by identity-provider
  claims via **OIDC / SAML**, **single sign-on (SSO)**, and **SCIM** user/group
  provisioning.
- **Tenancy & governance.** Per-tenant isolation guarantees, **audit trails**,
  retention/compliance controls, and per-tenant **quotas / rate limits**.
- **Operations.** An admin surface for principals, roles and policies, plus
  secrets/credential management — all **self-hostable**, no external identity
  provider required.

Until this lands, treat any enterprise/government use as **single-tenant on
trusted infrastructure**.

## Connector Engine

A generic engine to **connect Ironclad to third-party systems for information
retrieval and analysis** — pulling data from systems of record and analytics
platforms into the agent's reasoning and the retrieval/RAG layer, under
sovereign, governed control.

- **Contract-driven connectors.** Each connector is declared by a schema (ACK-
  style), with its own credential handling and a **read-first, governed egress**
  policy, so a connector can be pinned to retrieval-only and fully audited.
- **Pipeline integration.** Retrieved data flows into the memory tiers and the
  per-turn RAG assembly, so external knowledge becomes first-class context for
  analysis — without leaving your infrastructure.
- **First connector: SAP** — access to SAP systems of record (e.g. via
  OData / RFC / BAPI) for business-data retrieval and analysis.
- **Next: iView (Informatec, Switzerland)** — a Qlik-integrated **BI / data-
  automation framework** (iVIEW Library + Dataflow); the connector taps its
  governed BI datasets and data flows so curated business intelligence becomes
  context for the agent's analysis.
- **Further candidates.** Relational databases, SharePoint / Confluence,
  generic REST / GraphQL endpoints.

## Broader model & data reach

Staying independent means running on more of *your* models and *your* data.

- **More local open models.** Verified connection recipes for additional
  locally-served open models (beyond the reference Qwen / Falcon / Jais / K2
  set), so you can pick the model that fits your hardware and licensing.
- **RAG over local datasets.** Retrieval over your own document and data
  collections through the memory hook — your private corpus becomes queryable
  context, kept on-prem.
- **Richer cold-tier retrieval.** Continued growth of the long-term vector(+graph)
  store as the substrate both connectors and local datasets feed into.

## Hardening & release maturity

Maturing the project itself toward a dependable, versioned release.

- **Broader test coverage** and hardening of the server/client paths.
- **Automated release pipeline.** Formalising the internal DEV → Prod → Public
  promote path (today a manual gated path) into an automated, gated flow; the
  core stays inbound-closed.
- **A stable (1.0) release** once the APIs settle (today: tagged `0.0.x` alpha
  previews on PyPI `ironclad-ai` + GitHub Releases).

Issues and discussions are welcome — this is an early, openly-developed project.
