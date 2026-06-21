# Roadmap

> Forward-looking only: this is what is **planned or in progress** — none of it
> ships yet. For what already works today (the secure session-gated channel,
> governed parallelism, the provider router, the plugin surface, the terminal
> client, multi-tier memory), see [`status.md`](status.md). Items below are
> directions, not commitments, and may change.

Ironclad runs today as a sovereign, single-operator system — one principal,
home-LAN trust, code stays on your machine. The work below extends it along
several themes — **enterprise readiness**, **external-system connectors**,
**self-generating skills**, and **broader model/data reach** — without giving up
the core promise: self-hosted, model-agnostic, no vendor lock-in.

## 1. Enterprise & multi-tenant readiness

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

## 2. Connector Engine — governed access to external systems

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

## 3. Skill-generation engine & skill library

An engine that **generates skills automatically** and a curated library to share
them — turning a described capability into a working, tested plugin.

- **Automatic skill generation.** From a natural-language description or a spec,
  the engine emits a plugin against the open plugin contract (`CASE` + `run`),
  wired to the ACK so the generated tool is schema-validated by construction.
- **Quality by construction.** Each generated skill ships with auto-generated
  tests and must pass the **doctor preflight** before it is registered — no
  unchecked code enters the agent's toolset.
- **Skill library.** A versioned, **self-hosted** catalogue of reusable skills:
  discover, install and update from your own library (no mandatory external
  marketplace), with provenance so you stay in control of what runs.

Design: [ADR-0001](adr/0001-skill-engine-and-library.md) + [`skill-packaging.md`](skill-packaging.md)
(two skill kinds — typed `CASE`+`run` tools and `SKILL.md` playbooks; doctor+tests gate,
behavioral eval opt-in; manifest catalogue; `skills/mpr` migrated as the reference built-in).
Evolving (design): [ADR-0002](adr/0002-core-always-on-skills.md) — the skill/prompt engine + MPR
become **core, always-on** built-ins (loaded from a fixed core dir, independent of
`GX10_PLUGINS_DIR`); the plugin surface stays for 3rd-party skills; MPR de-plugined (runtime
`mpr.enabled`, default on).
Planned (design): [ADR-0003](adr/0003-prompt-library.md) + [`prompt-packaging.md`](prompt-packaging.md)
— a curated, **multilingual prompt library** + generator: a prompt is a declarative `kind: prompt`
core built-in (variables + languages + guided elicitation); `/<prompt-name>` → asked for inputs →
finished prompt in the target language; add a prompt by dropping an MD file.

## 4. Broader model & data reach

Staying independent means running on more of *your* models and *your* data.

- **More local open models.** Verified connection recipes for additional
  locally-served open models (beyond the reference Qwen / Falcon / Jais / K2
  set), so you can pick the model that fits your hardware and licensing.
- **RAG over local datasets.** Retrieval over your own document and data
  collections through the memory hook — your private corpus becomes queryable
  context, kept on-prem.
- **Richer cold-tier retrieval.** Continued growth of the long-term vector(+graph)
  store as the substrate both connectors and local datasets feed into.

## 5. Hardening & release maturity

Maturing the project itself toward a dependable, versioned release.

- **Broader test coverage** and hardening of the server/client paths.
- **Automated release pipeline.** Formalising the internal DEV → Prod → Public
  promote path (today a manual gated path: boundary + tests + docs + review +
  export) into an automated, gated flow; the core stays inbound-closed.
- **A stable (1.0) release** once the APIs settle (today: tagged `0.0.x` alpha previews
  on PyPI `ironclad-ai` + GitHub Releases).

Issues and discussions are welcome — this is an early, openly-developed project.
