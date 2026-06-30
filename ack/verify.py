"""Verifier / Evaluation Layer — MARK-ONLY behavioral evaluation (Agent-Contract-Kernel, #602 S602-4).

> **Evaluation beyond schema-validity, without ever gating.** The hard floor (`constrained_emission`) and
> the re-ask loop (`validated_emit`) guarantee a reply is *structurally* valid. This layer adds *behavioral*
> evaluation — does the output satisfy business rules, is each claim grounded in retrieved evidence, would an
> LLM judge pass it — and returns a :class:`VerdictResult`. **It is MARK-ONLY / advisory: a verdict can
> neither relax nor tighten any gate.** The fail-closed core path is untouched; verdicts are read only by the
> opt-in reflection layer (quality trends — #602 SUB-9), never by a gate.

Three pluggable verifiers, all **opt-in, default-off** (nothing runs unless a caller invokes it → byte-
identical) and **secret-free / transport-injected** (like `validated_emit`, auth lives in the injected
transport, never here):

  1. :func:`verify_rules`      — deterministic business-logic rules (pure predicates).
  2. :func:`verify_grounding`  — each claim grounded by an INJECTED ``retrieve`` (e.g. a cold-store hit).
  3. :func:`verify_with_judge` — an opt-in LLM-as-judge over the injected async ``chat`` transport, **budget-
     gated**: it charges an injected ledger (duck-typed ``can_afford``/``charge`` — the engine's
     ``dispatch.BudgetLedger``; ACK never imports the engine) and SKIPS the call when unaffordable.

Every verifier **never raises** — an error makes the check abstain/fail advisorily, never breaks a turn.
Imports only the stdlib (the budget ledger + chat transport are injected, never imported).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, Sequence, Tuple


@dataclass(frozen=True)
class VerdictResult:
    """A MARK-ONLY evaluation verdict — advisory, NEVER a gate.

    ``passed`` / ``score`` (0.0–1.0) / ``reason`` describe a behavioral check; ``verifier`` names its source.
    A ``VerdictResult`` carries NO gate semantics — it can neither permit nor block anything; consumers (the
    Quality breaker #602 SUB-9, observability) read it, the fail-closed core never does.
    """

    passed: bool
    score: float
    reason: str
    verifier: str = ""


def _clamp01(x: Any) -> float:
    """Coerce *x* to a float in [0, 1]; never raises (bad input → 0.0)."""
    try:
        f = float(x)
    except Exception:   # noqa: BLE001
        return 0.0
    if f != f:          # NaN
        return 0.0
    return 0.0 if f < 0.0 else 1.0 if f > 1.0 else f


def _safe_str(x: Any) -> str:
    """str(x) that never raises (a hostile __str__ → a placeholder name)."""
    try:
        return str(x)
    except Exception:   # noqa: BLE001
        return "<rule>"


def _safe_list(seq: Any) -> list:
    """Materialize *seq* into a list, never raising — a non-iterable / hostile iterator → ``[]``."""
    if seq is None:
        return []
    try:
        return list(seq)
    except Exception:   # noqa: BLE001 — a truthy non-iterable (object()) / a raising __iter__ → empty
        return []


#: A deterministic business-logic rule: ``(name, predicate)`` where ``predicate(value) -> truthy``.
Rule = Tuple[str, Callable[[Any], bool]]
#: Parses a chat-completion response dict into a VerdictResult (caller-supplied, judge-specific).
JudgeParser = Callable[[dict], "VerdictResult"]


def verify_rules(value: Any, rules: Optional[Sequence[Rule]], *, verifier: str = "rules") -> VerdictResult:
    """Run deterministic business-logic *rules* against *value*. A predicate that returns falsy OR raises is
    a FAIL for that rule (named in the reason). ``score`` = passed/total; ``passed`` = every rule passed.
    Pure + **never raises**. No rules ⇒ a vacuous pass (score 1.0)."""
    try:
        items = [r for r in _safe_list(rules) if isinstance(r, tuple) and len(r) == 2 and callable(r[1])]
        if not items:
            return VerdictResult(True, 1.0, "no rules", verifier)
        failed = []
        for name, pred in items:
            ok = False
            try:
                ok = bool(pred(value))
            except Exception:   # noqa: BLE001 — a rule that blows up is a fail, never a raise
                ok = False
            if not ok:
                failed.append(_safe_str(name))
        passed_n = len(items) - len(failed)
        reason = "all rules passed" if not failed else "failed: " + ", ".join(failed)
        return VerdictResult(not failed, passed_n / len(items), reason, verifier)
    except Exception:   # noqa: BLE001 — absolute never-raises: any pathological rule input → vacuous pass
        return VerdictResult(True, 1.0, "no rules", verifier)


def verify_grounding(
    claims: Optional[Sequence[str]],
    retrieve: Callable[[str], Any],
    *,
    threshold: float = 1.0,
    verifier: str = "grounding",
) -> VerdictResult:
    """Check each claim is grounded by the INJECTED ``retrieve(claim) -> truthy`` (e.g. a cold-store hit).
    A ``retrieve`` error → that claim counts as ungrounded. ``score`` = grounded/total; ``passed`` =
    ``score >= threshold`` (clamped to [0,1]). Pure given the injected retrieve, **never raises**. No
    (non-empty str) claims ⇒ a vacuous pass."""
    try:
        items = [c for c in _safe_list(claims) if isinstance(c, str) and c.strip()]
        if not items:
            return VerdictResult(True, 1.0, "no claims", verifier)
        grounded = 0
        if callable(retrieve):
            for c in items:
                try:
                    if retrieve(c):
                        grounded += 1
                except Exception:   # noqa: BLE001 — a retrieval error → ungrounded, never a raise
                    pass
        th = _clamp01(threshold)
        score = grounded / len(items)
        return VerdictResult(score >= th, score, f"{grounded}/{len(items)} grounded (>= {th:.2f})", verifier)
    except Exception:   # noqa: BLE001 — absolute never-raises: any pathological claim input → vacuous pass
        return VerdictResult(True, 1.0, "no claims", verifier)


async def verify_with_judge(
    *,
    chat: Callable[..., Awaitable[dict]],
    messages: list,
    parse: JudgeParser,
    budget: Any = None,
    cost: float = 0.0,
    cap: Optional[float] = None,
    model: Optional[str] = None,
    temperature: float = 0.0,
    verifier: str = "judge",
) -> Optional[VerdictResult]:
    """Opt-in LLM-as-judge over the INJECTED async ``chat`` transport (secret-free — auth lives in the
    transport). **Budget-gated**: when a *budget* ledger is given (duck-typed ``can_afford(cost, cap)`` /
    ``charge(cost)`` — the engine's ``dispatch.BudgetLedger``) and it cannot afford *cost* under *cap*, the
    judge is **SKIPPED** → returns ``None`` (no ``chat`` call, nothing charged). Otherwise it calls ``chat``,
    runs the caller's *parse* on the reply, and charges *cost* **only on a completed call that yields a valid**
    :class:`VerdictResult` — a transport/parse failure abstains (``None``) and charges **nothing**.

    **MARK-ONLY** (advisory) and **never raises**: a budget error, a transport error, or a parse error all
    return ``None`` (the judge abstains — it never blocks a turn). **Default-off**: a caller only invokes
    this when its config opts in, so with the judge unconfigured the path is byte-identical (never reached).
    """
    if budget is not None:
        try:
            if not budget.can_afford(cost, cap):
                return None             # budget gate: skip the call entirely — nothing is charged
        except Exception:   # noqa: BLE001 — a budget hiccup → abstain (advisory), never raise
            return None
    try:
        resp = await chat(messages=messages, model=model, temperature=temperature, extra_body={})
        verdict = parse(resp)
    except Exception:   # noqa: BLE001 — transport / parse failure → the judge abstains; NOTHING is charged
        return None
    if not isinstance(verdict, VerdictResult):
        return None                     # a bad parse result → abstain, nothing charged
    if budget is not None:
        try:
            budget.charge(cost)         # charge ONLY a completed judge call (no over-charge on failure)
        except Exception:   # noqa: BLE001 — a charge hiccup must not drop an already-produced valid verdict
            pass
    return verdict
