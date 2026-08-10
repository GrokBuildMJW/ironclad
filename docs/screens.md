# A look at the clients

Three ways to work with Ironclad, all watching the same run. These are
illustrative mockups, not screenshots — the shapes and data are
representative of what each client actually shows.

## Terminal client

Fast, keyboard‑first, built for staying in the flow of a session:

```
┌─ ironclad · project: billing-service ──────────────────────────── main ─┐
│                                                                          │
│  Board                                                                  │
│  ─────                                                                  │
│  ● scope          done                                                  │
│  ● design         done                                                  │
│  ▶ build          in progress   step 4/7 — rate_limiter.implement       │
│  ○ go-live        pending                                               │
│  ○ operate        pending                                               │
│                                                                          │
│  Turn                                                                   │
│  ────                                                                   │
│  you   › Add rate limiting to /api/v1/uploads. 10 req/min per account.  │
│                                                                          │
│  ironclad › Scope written. Design settled on a shared in-memory         │
│    limiter, sliding window. Building now — step 4 of 7                  │
│    (rate_limiter.implement). Tests for the 429 path are next.           │
│                                                                          │
│  [F2] board   [F3] approvals   [F5] logs   [Ctrl+C] cancel turn         │
└──────────────────────────────────────────────────────────────────────┘
```

## Rich terminal client

The same session, more surface at once — a live process map alongside the
approval queue and event feed:

```
╭─ ironclad ─ billing-service ──────────────────────────────────────────╮
│ PROCESS MAP                      │ APPROVALS                          │
│ ──────────                       │ ─────────                          │
│ ✔ scope                          │ ⧗ design_review_gate                │
│ ✔ design                         │    round 1/5 · awaiting reviewer    │
│ ▶ build              4/7         │                                     │
│   ├─ ✔ scope_authoring           │ RECENT EVENTS                      │
│   ├─ ✔ design_mpr                │ ───────────────                    │
│   ├─ ✔ design_review_gate        │ 12:41  step.committed               │
│   ├─ ▶ rate_limiter.implement    │        rate_limiter.implement       │
│   ├─ ○ rate_limiter.test         │ 12:40  gate.approved                │
│   ├─ ○ integration_check         │        design_review_gate           │
│   └─ ○ handoff                   │ 12:36  turn.queued                  │
│ ○ go-live                        │        "Add rate limiting to..."    │
│ ○ operate                        │                                     │
╰───────────────────────────────────────── ⏎ send · Tab switch · ? help ─╯
```

## Browser console

A read‑only window onto the same run, live over the same event stream —
useful for watching progress without a terminal open:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ●  ●  ●    ironclad.local/console/billing-service          ⟳    🔒    │
├──────────────────────────────────────────────────────────────────────┤
│  Ironclad Console                                     ● live · SSE   │
│ ┌────────────────┐  ┌────────────────────────────────────────────┐  │
│ │ PROJECTS        │  │  billing-service                Build 4/7 │  │
│ │  • billing-svc ●│  │  ────────────────────────────────────────  │  │
│ │    auth-gateway │  │  Plan ✔      Build ▶      Run ○            │  │
│ │    docs-portal  │  │                                             │  │
│ │                 │  │  Process map                                │  │
│ │ BOARD           │  │  scope ✔ → design ✔ → build ▶ → go-live ○  │  │
│ │  scope      ✔   │  │            → operate ○                     │  │
│ │  design     ✔   │  │                                             │  │
│ │  build      ▶   │  │  Live log                                   │  │
│ │  go-live    ○   │  │  12:41  step.committed  rate_limiter…       │  │
│ │  operate    ○   │  │  12:40  gate.approved   design_review_gate  │  │
│ └────────────────┘  │  12:36  turn.queued     "Add rate limiting…" │  │
│                      └────────────────────────────────────────────┘  │
│                                                    read-only observer │
└──────────────────────────────────────────────────────────────────────┘
```

All three are views onto the exact same run — the same board, the same
event stream, the same evidence. Pick whichever one fits where you're
working.
