# Playbook: a feature request, start to finish

This walks through a single, realistic example — adding rate limiting to an
API endpoint — from the first message to a deployed, verified change.

## 1. The ask

> "Add rate limiting to `/api/v1/uploads`. 10 requests per minute per
> account, return a proper 429 when it's exceeded."

That's the whole input. No ticket template, no acceptance‑criteria checklist
to fill out by hand.

## 2. Scope

Ironclad turns the request into a concrete plan: where the limiting logic
belongs, what "per account" resolves to in this codebase, what the 429
response body should contain, and what already exists that a new rate
limiter needs to compose with — auth middleware, existing error envelopes,
existing tests for the endpoint. The plan is written down before anything is
built, so it can be checked against the actual request, not reconstructed
after the fact from the diff.

## 3. Design

For anything with a real decision behind it — an in‑memory limiter versus a
shared store, a sliding window versus a fixed one — Ironclad works out the
tradeoff explicitly rather than picking silently. If the codebase already
has an established pattern for this kind of decision, it follows it; if
there's a genuine fork with no established answer, it says so rather than
guessing.

## 4. Build

Implementation happens step by step. Each step's own tests are written
alongside it — not appended afterward — and a new fail‑closed path (what
happens when the limit is hit) ships with a real counterfactual proof: the
limiter is deliberately broken, the test that should catch it is confirmed
to actually go red, then the fix is restored and confirmed green again. A
green suite by itself is never treated as proof; the failure has to be seen
to happen.

## 5. Go‑live

Before anything is considered done, the full test suite runs, the specific
new behavior is exercised against a real request (not just a unit‑level
mock), and the evidence — what ran, what passed, what the actual response
looked like under load — is attached to the change itself. Nothing ships on
the strength of a summary claiming it works.

## 6. Operate

Once live, the endpoint is watched: request volume, 429 rates, anything that
looks like the limiter is either too aggressive or not catching what it
should. If something goes wrong, it becomes an incident with its own record,
not a mystery someone has to reconstruct from scratch later.

## 7. What Ironclad remembers

If this run turned up something worth keeping — a subtlety in how this
codebase handles per‑account identity, a gotcha in how the existing error
envelope needed to be extended — that observation is reflected on, checked
against what's already known, and, if it survives that check, proposed as a
new piece of knowledge. It doesn't become part of the system's working
memory until an explicit approval says so. The next time a similar request
comes in — for this project, or for the parts of the lesson that generalize
— that knowledge is there, ranked by how relevant and how recent it is, not
just dumped into context because it happens to exist.

---

That's the shape of every run: a real plan, a real decision trail, evidence
instead of summaries, and a system that gets a little better at this exact
kind of work every time it does it again.
