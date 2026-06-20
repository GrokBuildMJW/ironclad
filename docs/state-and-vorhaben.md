# State layout & vorhaben

Ironclad keeps a hard line between **the code you are working on** and **the state the engine
produces**. Nothing is scattered into your project root: engine machinery is hidden, and every
artifact belongs to an explicit *vorhaben* (German for "undertaking" — a named unit of work).

## The two roots

```
<workdir>/                       # your code — the agent reads/writes code here
  .ironclad/                     # ENGINE MACHINERY (hidden, vorhaben-independent)
    session.json                 #   the orchestrator's LLM context (survives a restart)
    memory/                      #   local warm-cache scratch (the real store is the Mem0 service)
    config.json                  #   install-type marker (written by the installer/deploy layer, not the engine)
    active                       #   slug of the active vorhaben (one line)
    agent/                       #   local code-agent scratch (handover/feedback drop zone)
  vault/                         # KNOWLEDGE (visible, vorhaben-centric — navigable like Obsidian)
    <slug>/                      #   one vorhaben
      meta.md                    #     frontmatter: typ, titel, erstellt, status
      INDEX.md                   #     AUTO (reconcile): overview + [[links]] — never hand-edited
      decisions/  proposals/  reviews/      # visible artifacts (typ=software)
      tasks/{pending,in_progress,done}/     #   the deterministic TaskStore
      runs/<run-id>/             #     reasoning runs (typ=mpr: perspectives + synthesis + manifest)
      .work/                     #     hidden machine plumbing
        active.md                #       the active handover (a projection, never hand-edited)
        handovers/  feedback/    #       the live inbox the reconciler advances on
        archive/                 #       handover + feedback history
```

`.ironclad/` is engine machinery; `vault/<slug>/` is where every produced artifact lives. The
project root stays clean — only `.ironclad/`, `vault/`, and your code.

Both roots are workdir-relative and overridable:

| Config key | Env | Default | Meaning |
|------------|-----|---------|---------|
| `paths.state_root` | — | `.ironclad` | hidden engine-machinery root |
| `paths.vault_root` | — | `vault` | visible knowledge root |
| `paths.session_file` | — | `session.json` | resolved under `state_root` (absolute path → used verbatim) |

## Vorhaben

A vorhaben is created **explicitly** — there is no artifact-producing operation without an active
vorhaben (fail-closed). Pure conversational turns (no artifacts) need none.

```text
/vorhaben new <name> --typ mpr|software   create + activate (writes meta.md + the typ skeleton)
/vorhaben list                            all vorhaben (* = active)
/vorhaben use <slug>                      switch the active vorhaben
/vorhaben active                          show the active vorhaben
/vorhaben reconcile [slug]                rebuild INDEX.md + [[links]] (see below)
```

- **`--typ software`** seeds `tasks/`, `decisions/`, `proposals/`, `reviews/`, and the hidden
  `.work/` plumbing — the full task → handover → feedback → done pipeline.
- **`--typ mpr`** seeds `runs/` and `decisions/` — reasoning runs only.
- The slug is derived from the name (kebab-case, German umlauts folded, collision-suffixed).
- The **active** vorhaben (a slug in `.ironclad/active`) is the routing target. The **engine-routed**
  artifacts — the `TaskStore`, the `stage_handover` / `advance_pipeline` plumbing, and MPR `runs_dir` —
  resolve relative to it; `decisions/`, `proposals/`, `reviews/` are seeded dirs the agent writes into
  (no dedicated engine router). Switching the active vorhaben switches the whole task view.

Artifact-producing operations are **fail-closed**: with no active vorhaben they return a clear
"kein aktives Vorhaben — `/vorhaben new …` zuerst" instead of writing into the project root.
The reconciler and the autopilot poller soft-skip when no vorhaben is active — they never crash the
daemon. (The `/doctor` self-check is independent of vorhaben state.)

## Self-maintaining vault (`reconcile_vault`)

The vault keeps itself navigable **deterministically — no model call**, the same idea as a
hand-rolled `MEMORY.md` index:

- **`INDEX.md`** is regenerated from the docs' frontmatter (grouped by category, newest first,
  with Obsidian `[[links]]`). It lives between AUTO markers, so any prose you add outside the block
  survives. The hidden `.work/` plumbing is never indexed.
- A **"Verwandt (auto)"** block is injected into the curated docs (`decisions/`, `proposals/`,
  `reviews/`) linking related docs — same frontmatter tags, or a title referenced in the body.
  It is idempotent (re-running changes nothing) and is tidied away when the relation disappears.

Reconcile runs automatically after a write (`vorhaben new`, `stage_handover`, `advance_pipeline`,
an MPR run) in **index-only** mode — it keeps `INDEX.md` fresh without touching doc bodies (so it
never fights an open editor). The full pass, including the `[[links]]` injection into bodies, runs
on the explicit `/vorhaben reconcile`.

## Migration

There is no migration step. New work uses the new structure; any previously scattered state
(`tasks/`, `summaries/`, …) in an old workdir is simply left in place. This is a fresh-start
design for a development tool.
