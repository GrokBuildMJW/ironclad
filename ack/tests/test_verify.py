"""Verifier / Evaluation Layer — mark-only behavioral evaluation (ACK, #602 S602-4).

Proves, offline (no model, no live store):

  * `verify_rules` aggregates deterministic predicates (a raising rule is a fail, never a raise);
  * `verify_grounding` scores claims against an injected `retrieve` (a retrieve error → ungrounded);
  * `verify_with_judge` is opt-in over the injected async `chat`, **budget-gated** (skips + does not charge
    when unaffordable; charges + calls when affordable), and **never raises** (transport/parse error → None);
  * `VerdictResult` is mark-only data (no gate semantics) — and the whole module is default-off byte-identical
    (nothing runs unless invoked).

    python -m pytest ack/tests/test_verify.py -q
"""
from __future__ import annotations

import asyncio

import pytest

from ack.verify import (
    VerdictResult,
    verify_grounding,
    verify_rules,
    verify_with_judge,
)


def _run(coro):
    return asyncio.run(coro)


class _Ledger:
    """A duck-typed stand-in for the engine's dispatch.BudgetLedger."""

    def __init__(self, spent=0.0):
        self.spent = spent
        self.charged = []

    def can_afford(self, cost, cap):
        return cap is None or self.spent + cost <= cap

    def charge(self, cost):
        self.spent += cost
        self.charged.append(cost)
        return self.spent


def _judge_response(passed: bool, score: float):
    return {"verdict": {"passed": passed, "score": score}}


def _parse(resp) -> VerdictResult:
    v = resp["verdict"]
    return VerdictResult(bool(v["passed"]), float(v["score"]), "judged", "judge")


# ─── verify_rules ─────────────────────────────────────────────────────────────────────────────────
def test_rules_all_pass():
    v = verify_rules(10, [("positive", lambda x: x > 0), ("even", lambda x: x % 2 == 0)])
    assert v.passed is True and v.score == 1.0


def test_rules_partial_fail_scores_and_names():
    v = verify_rules(3, [("positive", lambda x: x > 0), ("even", lambda x: x % 2 == 0)])
    assert v.passed is False
    assert v.score == 0.5
    assert "even" in v.reason and "positive" not in v.reason


def test_rules_raising_predicate_is_a_fail_not_a_raise():
    def _boom(x):
        raise RuntimeError("nope")
    v = verify_rules(1, [("boom", _boom)])
    assert v.passed is False and v.score == 0.0


def test_rules_empty_is_vacuous_pass():
    assert verify_rules(1, []).passed is True
    assert verify_rules(1, None).score == 1.0


def test_rules_garbage_non_iterable_never_raises():
    # a truthy non-iterable / a non-tuple entry must not raise → degrades to "no rules".
    assert verify_rules(1, object()).passed is True
    assert verify_rules(1, [("bad-shape",), 5, "x"]).passed is True


def test_rules_hostile_name_str_never_raises():
    class _BadName:
        def __str__(self):
            raise RuntimeError("nope")
    v = verify_rules(1, [(_BadName(), lambda x: False)])    # the rule fails; naming it must not raise
    assert v.passed is False


def test_rules_hostile_tuple_len_never_raises():
    class _BadTuple(tuple):
        def __len__(self):
            raise RuntimeError("nope")
    v = verify_rules(1, [_BadTuple()])                      # hostile __len__ in the filter → outer guard
    assert isinstance(v, VerdictResult)                    # no raise


# ─── verify_grounding ─────────────────────────────────────────────────────────────────────────────
def test_grounding_all_grounded():
    v = verify_grounding(["a", "b"], lambda c: True)
    assert v.passed is True and v.score == 1.0


def test_grounding_partial_below_threshold_fails():
    v = verify_grounding(["a", "b"], lambda c: c == "a")   # 1/2 grounded
    assert v.score == 0.5 and v.passed is False            # default threshold 1.0


def test_grounding_threshold_allows_partial():
    v = verify_grounding(["a", "b"], lambda c: c == "a", threshold=0.5)
    assert v.passed is True


def test_grounding_retrieve_error_counts_ungrounded():
    def _boom(c):
        raise RuntimeError("store down")
    v = verify_grounding(["a"], _boom)
    assert v.score == 0.0 and v.passed is False           # no raise


def test_grounding_no_claims_is_vacuous_pass():
    assert verify_grounding([], lambda c: False).passed is True
    assert verify_grounding(["  ", 5], lambda c: False).passed is True   # only non-empty strs count


def test_grounding_garbage_non_iterable_never_raises():
    assert verify_grounding(object(), lambda c: True).passed is True     # degrades to "no claims"


def test_grounding_non_callable_retrieve_never_raises():
    v = verify_grounding(["a"], None)                                    # not callable → ungrounded, no raise
    assert v.score == 0.0 and v.passed is False


def test_grounding_hostile_str_strip_never_raises():
    class _BadStr(str):
        def strip(self, *a):
            raise RuntimeError("nope")
    v = verify_grounding([_BadStr("a")], lambda c: True)                 # hostile strip() → outer guard
    assert isinstance(v, VerdictResult)                                 # no raise


# ─── verify_with_judge (opt-in, budget-gated, never-raises) ────────────────────────────────────────
def test_judge_returns_verdict_when_affordable():
    async def chat(*, messages, model=None, temperature=None, extra_body=None):
        return _judge_response(True, 0.9)
    led = _Ledger()
    v = _run(verify_with_judge(chat=chat, messages=[], parse=_parse, budget=led, cost=0.01, cap=1.0))
    assert isinstance(v, VerdictResult) and v.passed is True and v.score == 0.9
    assert led.charged == [0.01]                          # charged exactly the cost


def test_judge_skipped_when_unaffordable_and_not_charged():
    called = {"n": 0}
    async def chat(*, messages, model=None, temperature=None, extra_body=None):
        called["n"] += 1
        return _judge_response(True, 1.0)
    led = _Ledger(spent=1.0)
    v = _run(verify_with_judge(chat=chat, messages=[], parse=_parse, budget=led, cost=0.5, cap=1.0))
    assert v is None                                      # skipped
    assert called["n"] == 0                               # no chat call
    assert led.charged == []                              # nothing charged


def test_judge_without_budget_runs():
    async def chat(*, messages, model=None, temperature=None, extra_body=None):
        return _judge_response(False, 0.2)
    v = _run(verify_with_judge(chat=chat, messages=[], parse=_parse))
    assert v.passed is False and v.score == 0.2


def test_judge_charges_nothing_on_transport_failure():
    """A transport failure AFTER the affordability gate must charge NOTHING (no over-charge for work that
    did not complete) — charge happens only on a completed, valid verdict."""
    async def chat(*, messages, model=None, temperature=None, extra_body=None):
        raise RuntimeError("upstream 502")
    led = _Ledger()
    v = _run(verify_with_judge(chat=chat, messages=[], parse=_parse, budget=led, cost=0.5, cap=1.0))
    assert v is None and led.charged == []


def test_judge_charges_nothing_on_bad_parse():
    async def chat(*, messages, model=None, temperature=None, extra_body=None):
        return {"unexpected": "shape"}
    led = _Ledger()
    v = _run(verify_with_judge(chat=chat, messages=[], parse=_parse, budget=led, cost=0.5, cap=1.0))
    assert v is None and led.charged == []           # bad parse → abstain, nothing charged


def test_judge_transport_error_returns_none():
    async def chat(*, messages, model=None, temperature=None, extra_body=None):
        raise RuntimeError("upstream 502")
    v = _run(verify_with_judge(chat=chat, messages=[], parse=_parse))
    assert v is None                                      # abstains, never raises


def test_judge_parse_error_returns_none():
    async def chat(*, messages, model=None, temperature=None, extra_body=None):
        return {"unexpected": "shape"}
    v = _run(verify_with_judge(chat=chat, messages=[], parse=_parse))
    assert v is None


def test_judge_non_verdict_parse_result_is_none():
    async def chat(*, messages, model=None, temperature=None, extra_body=None):
        return _judge_response(True, 1.0)
    v = _run(verify_with_judge(chat=chat, messages=[], parse=lambda r: "not a verdict"))
    assert v is None


def test_judge_non_verdict_charges_nothing():
    async def chat(*, messages, model=None, temperature=None, extra_body=None):
        return _judge_response(True, 1.0)
    led = _Ledger()
    v = _run(verify_with_judge(chat=chat, messages=[], parse=lambda r: "not a verdict",
                               budget=led, cost=0.5, cap=1.0))
    assert v is None and led.charged == []      # a non-VerdictResult parse → abstain, nothing charged


def test_judge_charge_error_still_returns_verdict():
    class _ChargeBoom:
        def can_afford(self, cost, cap): return True
        def charge(self, cost): raise RuntimeError("ledger broke")
    async def chat(*, messages, model=None, temperature=None, extra_body=None):
        return _judge_response(True, 0.9)
    v = _run(verify_with_judge(chat=chat, messages=[], parse=_parse, budget=_ChargeBoom(), cost=0.1))
    assert isinstance(v, VerdictResult) and v.passed is True   # a charge hiccup must not drop a valid verdict


def test_judge_budget_error_abstains():
    class _BadLedger:
        def can_afford(self, cost, cap):
            raise RuntimeError("ledger broke")
    async def chat(*, messages, model=None, temperature=None, extra_body=None):
        return _judge_response(True, 1.0)
    v = _run(verify_with_judge(chat=chat, messages=[], parse=_parse, budget=_BadLedger(), cost=0.1))
    assert v is None


# ─── VerdictResult is mark-only data ───────────────────────────────────────────────────────────────
def test_verdict_is_frozen_and_has_no_gate_field():
    v = VerdictResult(True, 1.0, "ok", "rules")
    with pytest.raises(Exception):
        v.passed = False                                 # frozen
    # mark-only: the dataclass carries no blocking/gate attribute.
    assert not any(f in VerdictResult.__dataclass_fields__ for f in ("blocking", "gate", "blocks"))
