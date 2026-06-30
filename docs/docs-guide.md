# Documentation guide — where does this go?

Ironclad's docs have **one responsibility each, no overlap**. This keeps them honest and lets the
`doc-reality-audit` enforce the boundaries mechanically. Design: [ADR-0006](adr/0006-docs-ia-and-drift-proof-roadmap.md).

## One responsibility per doc

| Doc | Owns | Must NOT contain |
|-----|------|------------------|
| **`README.md`** | Intro, value proposition, quickstart / install, pointers. | The exhaustive feature/wiring matrix (link to `status.md` instead). |
| **`docs/status.md`** | The **wiring SSOT** — what actually works *right now*, per component. | Future/aspirational wording (`planned`, `coming soon`, `roadmap`, `will ship`). |
| **`docs/roadmap.md`** | **Future / unrealized only** — what's planned or in progress. **Generated** from open milestones (do not hand-edit). | Realized wording (`shipped`, `available`, `delivered`, `done`, `wired + tested`, `Delivered:`). |
| **`CHANGELOG.md`** | History — Keep-a-Changelog, per release + `[Unreleased]`. | Roadmap/aspiration; only what changed. |
| **`docs/*` (guides)** | Task / reference / explanation (Diátaxis, adopted incrementally). | Duplicated status/roadmap content. |
| **`docs/adr/*`** | Design **decisions** (context → decision → consequences). | Status tracking (link to `status.md`). |

## Where does this go? (decision guide)

- **"We built X / X works now"** → `status.md` (wiring row) + a `CHANGELOG` `[Unreleased]` entry. **Not** the roadmap.
- **"We plan to build X"** → it belongs to a **milestone** (a roadmap phase); the phase appears in `roadmap.md` **automatically** (generated from the open milestone's description). Open an epic for the concrete work; don't hand-write it into the roadmap.
- **"A whole phase is delivered"** → **close its milestone** → the phase **drops from the roadmap automatically**; record the delivery in `status.md` + `CHANGELOG` (this is the C2 prune-on-close gate).
- **"Why did we choose Y?"** → a new ADR under `docs/adr/`.
- **"How do I do Z?"** → a guide under `docs/` (Diátaxis: tutorial / how-to / reference / explanation).

## The roadmap is generated

`docs/roadmap.md` is produced by `scripts/ci/gen_roadmap.py` from the **open milestones** (the
roadmap phases): one section per open milestone, its **description** as the phase narrative. A
delivered phase disappears automatically when its **milestone is closed** — so realized work can
never linger there. Regenerate it when milestones change; CI (`roadmap-generated`) verifies the
committed file matches a fresh regeneration. To put planned work on the roadmap, give the milestone
a description (and open an epic for the concrete work); to retire a phase, close its milestone.

## Enforcement

`scripts/ci/doc_reality_audit.py` gates the machine-checkable slice: internal links/anchors,
version consistency, banned stale phrases, cross-doc number agreement, required docs present, and
the **per-doc responsibility lint** above (roadmap-forward-only + status-now-only). Plus `scripts/ci/process_doctor.py` asserts the live-state invariants (incl. an open milestone with
work but no description -> invisible on the roadmap, warned), and the release gate (`promote.sh`) runs
`gen_roadmap.py --check` so a release can never ship a stale roadmap. The full audit procedure (the
human, semantic part) lives in `vault/Plan/doc-reality-audit.md` (private).
