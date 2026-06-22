# ADR-0006 — Documentation IA + drift-proof, generated roadmap

- **Status:** Accepted (design) — implementation under epic #169 (sub-issues #171 IA, #172 roadmap migration, #173 reconcile, #174 audit lint, #175 prune-on-close gate, #176 roadmap generator). For what ships see [`status.md`](../status.md).
- **Date:** 2026-06-22
- **Context sources:** `docs/{roadmap.md,status.md}`, `core/README.md`, `core/CHANGELOG.md`, `scripts/ci/doc_reality_audit.py` (today's checks), `scripts/ci/export_core.py`, `.github/DEV_LOOP.md` (C2/E5 + the top doc rule), and prior anti-drift rounds #128 (steering rule) + #130/#131 (audit existence/version guard).

## Context

Documentation drift keeps recurring despite two rounds of fixes. Concrete, current evidence:
`roadmap.md` opens with *"Forward-looking only … none of it ships yet"* yet its body carries **realized** work — `Delivered: ADR-0003 … ADR-0005`, `Evolving: ADR-0002` (shipped), the shipped **Extension SDK** (ADR-0004, v0.0.12), and Phase 6 *"the usability & seed foundation **has shipped**"*. The two fully-closed milestones **M3** and **M6** (0 open issues) still appear as roadmap phases. `doc_reality_audit.py` passes anyway (it checks links/version/banned-phrases/cross-doc-numbers + doc existence/version) because it has **no notion of "realized"**.

Root cause — **structural, not cosmetic**: (1) the audit can't tell realized from planned; (2) moving a delivered item out of the roadmap is a **manual, gateless** step in the autonomous C1 loop, so it gets left behind; (3) doc responsibilities overlap and are prose, not machine-checkable. #128/#130/#131 fixed symptoms (docs exist, status names the version), not the structure.

## Decisions

**D1 — One responsibility per doc (machine-checkable).**
- **README.md** — intro, value proposition, quickstart/install. No exhaustive feature/wiring matrix (points to status.md).
- **status.md** — the **wiring SSOT**: what runs *now*. No "planned / coming soon / roadmap".
- **roadmap.md** — **future/unrealized only**, and **generated** (D2). No "shipped / available / delivered / done / wired + tested".
- **CHANGELOG.md** — Keep-a-Changelog history.
- **docs/** — task / reference / explanation; adopt **Diátaxis incrementally** (not a big-bang reorg — explicitly deferred).
- **ADRs** — design decisions.
A contributor-facing IA doc (`docs/docs-guide.md`, #171) states this + a "where does this go?" table.

**D2 — roadmap.md is GENERATED from the open phases (Option A — ratified).** `scripts/ci/gen_roadmap.py` writes `docs/roadmap.md` from GitHub state:
- **A phase = an OPEN milestone with a non-empty description.** The milestone *is* the roadmap phase; its **description** is the phase narrative. Rendered one `## <title>` section per open milestone, ordered by milestone number. (Refined at the epic-#169 C2 from an earlier "open milestone **with ≥1 open epic**" rule: that erased an active phase the moment its current epics merged — e.g. M5 would vanish when epic #169 itself closed. A phase's life is the milestone's, not its current epics'.)
- The file carries a "generated — do not edit by hand; run `gen_roadmap.py`" header.
- **Phase prose lives in milestone descriptions** (the single editable source; no private issue numbers reach the public export); the generator is the only writer of roadmap.md.

**D3 — Prune-on-close becomes structural + a C2 gate.** A delivered phase drops out the moment its **milestone is closed** — so a fully-delivered phase (its milestone has no remaining open work) is pruned by closing the milestone (e.g. M3 + M6, closed at the #169 C2). The roadmap is regenerated and the prune is automatic. DEV_LOOP **C2** (#175) mandates, at epic close: record the epic's delivered specifics in `status.md`/`CHANGELOG` (the durable record), regenerate the roadmap, and **close the milestone when its phase is fully delivered**. NEW_EPIC's C2 skeleton carries the same step.

**D4 — Two enforcement layers.**
1. **Offline, deterministic (doc_reality_audit, #174):** `roadmap.md` must contain no realized markers; `status.md` no future markers; README no exhaustive wiring matrix. Tiny canonical marker lists (like `BANNED_PHRASES`), with a **negative test** proving the audit FAILS on a deliberately-realized roadmap item (and PASSES clean). Runs on the export staging tree as today.
2. **Generation check (#176):** a private CI step (`roadmap-generated`) regenerates the roadmap and asserts the committed file is identical (drift = a milestone opened/closed or its description edited without a regen). Network/`gh`-dependent → **soft-skip on API error, hard-fail on a real diff** (a private convention gate; the offline lint is the always-on guard).

**D5 — Migration (no loss, no double-keeping).** The realized content currently in roadmap.md (ADR-0002/0003/0005 deliveries, the Extension SDK block, "has shipped") is verified present in `status.md` (wiring SSOT) + `CHANGELOG` (history), then removed from the roadmap by adopting the generated output (#172). Theme prose moves into milestone descriptions (#176). README is trimmed to intro/quickstart (#173).

## Boundary / security

`gen_roadmap.py` + the generation check live in `scripts/ci/` (**private**, never exported) and run in the **private** repo CI (which can read its own issues via `GITHUB_TOKEN`). The **output** `docs/roadmap.md` is a static, generated snapshot that is exported to `ironclad` — it renders epic **titles + milestone descriptions only**, never private issue numbers, never secrets. English-only. No engine/runtime behavior change.

## Consequences

- "Realized in the roadmap" becomes **structurally impossible** (generated from open epics) and **machine-caught** (offline lint + generation check) — drift can't silently return.
- Roadmap prose is terser/derived (milestone descriptions + open-epic titles) rather than hand-curated long-form — an accepted trade for accuracy; the durable narrative lives in status.md/CHANGELOG/ADRs.
- Discipline cost drops: closing an epic prunes the roadmap automatically; the loop just regenerates.

## Alternatives considered

- **Option B — handwritten roadmap + audit requires every item to reference an OPEN issue** — rejected as the primary mechanism (prune stays a manual, if gated, step; less drift-proof than generation). The open-issue-reference idea survives as a sanity check inside the generation model.
- **Full Diátaxis reorg of `docs/*` now** — deferred (large migration/risk in one epic; the responsibility-split + lint fixes the drift without it).
- **Keep prose in an in-repo `roadmap.data.*` file** instead of milestone descriptions — viable, but adds a second hand-maintained source; milestone descriptions are already the natural per-phase home and keep GitHub as the single planning SSOT.
