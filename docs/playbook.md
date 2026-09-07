# Playbook: a feature request, start to finish

This follows a realistic ask through the shipped software-development
rulebook — the fourteen-step `sw_dev_default` process, not a slide titled
Plan → Build → Run. Those three words are still a good way to *talk* about
it. This page is what ironclad-ai actually does.

See [Architecture](architecture.md) for skills, learning, and memory.
See [Processes](processes.md) for the other shipped definitions.

## 1. The ask

> Add rate limiting to `/api/v1/uploads`. 10 requests per minute per
> account. Return a proper 429 when it is exceeded.

That is the whole input. No ticket template. You type it in Ink.

## 2. Intake and spine

The engine records the request as Intake. A weak-prompt review is advisory:
a vague ask does not deadlock the run, but it is on the record.

Then the **spine** — language and approach — is written down *before* scope.
On the default rulebook this is an operator gate. Ink waits. You approve a
spine, or you send the run back. The browser console will show the wait. It
will not let you click it away.

## 3. Scope

Scope turns the ask into something checkable: where the limiter belongs,
what "per account" means in *this* codebase, what the 429 body looks like,
what already exists that the new code has to compose with.

On `sw_dev_default`, scope is dispatched and operator-gated. You read it.
You approve it. A replacement scope cannot ride an old approval.

## 4. Scope review

An independent review job runs against the persisted scope. Unanimous
approval continues. Findings reroute back to scope authoring, up to three
times, with review-of-review on. Exhaustion waits rather than pretending
the findings were optional.

## 5. Ops contract

Before design, the run authors an operations contract: deploy, health,
rollback, smoke, verification entrypoints. The default gate is **required**.
A missing smoke command or an invalid verification section aborts. This is
the opposite of "we'll add a Dockerfile later."

## 6. Design

For a real fork — in-memory limiter versus a shared store, sliding window
versus fixed — ironclad-ai works the tradeoff explicitly. The design step
fans the question out across several lenses and synthesizes one design.

Design review is the same shape as scope review: independent jobs, bounded
reroute to design, review-of-review. On the default rulebook, design
approval is an operator gate. `sw_dev_base` can auto-accept after review;
the default does not.

## 7. Units

The approved design is sliced into delivery units. Decomposition review
checks that the slice is the work you actually approved, then reroutes to
planning if it is not.

## 8. Execution

Implementation applies the change, runs the declared build, then the
declared tests, in that order. A new fail-closed path is expected to ship
with a counterfactual: the limiter is broken on purpose, the test that
should catch it goes red, the fix is restored, the suite goes green. A
green suite by itself is not treated as proof that the failure was seen.

Refusals retry up to the step budget, then follow the pinned refusal route.

## 9. Code review

Review jobs fan out over the actual diff and the verification evidence.
Findings reroute to execution. Review-of-review can overturn a verdict.
A remaining rejection can mint a learning proposal. An unchallenged approve
does not.

You still do not approve code review from the browser. Ink is the control
surface.

## 10. Go-live

The local go-live gate checks the contract you already approved. A failed
go-live can take an audited waiver. It cannot take a shrug.

## 11. What ironclad-ai remembers

If this run turned up something worth keeping — how this codebase identifies
an account, how the error envelope had to grow — that observation is
reflected, curated against what is already known, and proposed. It does not
become working memory until an approval says so.

The next similar ask retrieves by relevance and recency, not by dumping
everything that happens to exist.

---

That is the shape of a default software run: a written spine, a written
scope, independent review, an ops contract that can abort, a design you
can point at, tests that had to go red, and a ledger of who allowed the
run to move.
