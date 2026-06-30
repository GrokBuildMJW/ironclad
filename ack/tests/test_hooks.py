"""Tests for ``ack.hooks`` — the Loop-Intelligence Hook-Bus (#602 Teil-2 2.0, the keystone).

Covers the contract the C2 consumers depend on: byte-identical default, deterministic order, fail-loud
registration, fail-soft + observer-only dispatch, copy-on-write under a mid-dispatch registration,
cancel/budget early-out, and thread-safe registration.
"""
import threading
import time

import pytest

from ack import hooks


@pytest.fixture(autouse=True)
def _clean_bus():
    hooks.clear_hooks()
    yield
    hooks.clear_hooks()


def test_byte_identical_default_no_hooks():
    # No registration → dispatch is an O(1) no-op (the byte-identical default path); never raises.
    assert hooks.registered_events() == ()
    assert hooks.dispatch(hooks.POST_GENERATE, {"x": 1}) is None
    assert hooks.hook_count(hooks.POST_GENERATE) == 0


def test_register_and_dispatch_in_registration_order():
    seen = []
    hooks.register_hook(hooks.POST_GENERATE, lambda c: seen.append(("a", c)))
    hooks.register_hook(hooks.POST_GENERATE, lambda c: seen.append(("b", c)))
    hooks.dispatch(hooks.POST_GENERATE, 42)
    assert seen == [("a", 42), ("b", 42)]


def test_unknown_event_is_fail_loud():
    with pytest.raises(ValueError):
        hooks.register_hook("not_an_event", lambda c: None)


def test_non_callable_is_fail_loud():
    with pytest.raises(TypeError):
        hooks.register_hook(hooks.PRE_TURN, 123)


def test_dedup_by_identity():
    calls = []

    def fn(c):
        calls.append(1)

    hooks.register_hook(hooks.PRE_TURN, fn)
    hooks.register_hook(hooks.PRE_TURN, fn)  # same callable → no double registration
    assert hooks.hook_count(hooks.PRE_TURN) == 1
    hooks.dispatch(hooks.PRE_TURN)
    assert calls == [1]


def test_fail_soft_one_bad_hook_does_not_break_siblings():
    seen = []

    def boom(c):
        raise RuntimeError("nope")

    hooks.register_hook(hooks.POST_FEEDBACK, boom)
    hooks.register_hook(hooks.POST_FEEDBACK, lambda c: seen.append("ok"))
    assert hooks.dispatch(hooks.POST_FEEDBACK) is None  # must not raise
    assert seen == ["ok"]


def test_observer_only_return_value_ignored():
    hooks.register_hook(hooks.PRE_ADVANCE, lambda c: "abort?")  # a return can never relax a gate
    assert hooks.dispatch(hooks.PRE_ADVANCE) is None


def test_copy_on_write_register_during_dispatch():
    order = []

    def first(c):
        order.append("first")
        # register a new hook mid-dispatch — must NOT run in this in-flight (snapshotted) dispatch
        hooks.register_hook(hooks.PRE_TURN, lambda c2: order.append("late"))

    hooks.register_hook(hooks.PRE_TURN, first)
    hooks.dispatch(hooks.PRE_TURN)
    assert order == ["first"]               # "late" did not fire in this dispatch
    hooks.dispatch(hooks.PRE_TURN)          # next dispatch sees both
    assert order == ["first", "first", "late"]


def test_clear_one_event_and_all():
    hooks.register_hook(hooks.PRE_TURN, lambda c: None)
    hooks.register_hook(hooks.POST_GENERATE, lambda c: None)
    hooks.clear_hooks(hooks.PRE_TURN)
    assert hooks.hook_count(hooks.PRE_TURN) == 0
    assert hooks.hook_count(hooks.POST_GENERATE) == 1
    hooks.clear_hooks()
    assert hooks.registered_events() == ()


def test_should_cancel_stops_dispatch_before_first_hook():
    seen = []
    hooks.register_hook(hooks.POST_GENERATE, lambda c: seen.append(1))
    hooks.register_hook(hooks.POST_GENERATE, lambda c: seen.append(2))
    hooks.dispatch(hooks.POST_GENERATE, should_cancel=lambda: True)
    assert seen == []


def test_broken_cancel_check_is_swallowed():
    seen = []

    def bad_cancel():
        raise RuntimeError("x")

    hooks.register_hook(hooks.POST_GENERATE, lambda c: seen.append(1))
    hooks.dispatch(hooks.POST_GENERATE, should_cancel=bad_cancel)
    assert seen == [1]  # a broken cancel-check must not stop dispatch


def test_budget_stops_remaining_hooks():
    seen = []

    def slow(c):
        seen.append("slow")
        time.sleep(0.02)

    hooks.register_hook(hooks.POST_HANDOVER, slow)
    hooks.register_hook(hooks.POST_HANDOVER, lambda c: seen.append("after"))
    hooks.dispatch(hooks.POST_HANDOVER, budget_s=0.001)
    # budget is checked BEFORE each hook on cumulative elapsed: "slow" runs, then the budget is spent.
    assert seen == ["slow"]


def test_registered_events_sorted():
    hooks.register_hook(hooks.PRE_TURN, lambda c: None)
    hooks.register_hook(hooks.POST_GENERATE, lambda c: None)
    assert hooks.registered_events() == tuple(sorted([hooks.PRE_TURN, hooks.POST_GENERATE]))


def test_concurrent_registration_is_thread_safe():
    def reg(i):
        hooks.register_hook(hooks.POST_FEEDBACK, lambda c, i=i: None)

    threads = [threading.Thread(target=reg, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert hooks.hook_count(hooks.POST_FEEDBACK) == 20  # 20 distinct callables, lock-serialized


def test_all_canonical_events_accept_a_hook():
    for ev in hooks.HOOK_EVENTS:
        hooks.register_hook(ev, lambda c: None)
    assert set(hooks.registered_events()) == set(hooks.HOOK_EVENTS)


def test_unregister_removes_only_the_target_hook():
    seen = []
    a = lambda c: seen.append("a")
    b = lambda c: seen.append("b")
    hooks.register_hook(hooks.PRE_HANDOVER, a)
    hooks.register_hook(hooks.PRE_HANDOVER, b)
    hooks.unregister_hook(hooks.PRE_HANDOVER, a)      # remove only a; b (a sibling) stays
    assert hooks.hook_count(hooks.PRE_HANDOVER) == 1
    hooks.dispatch(hooks.PRE_HANDOVER)
    assert seen == ["b"]


def test_unregister_last_hook_drops_the_event():
    fn = lambda c: None
    hooks.register_hook(hooks.PRE_HANDOVER, fn)
    hooks.unregister_hook(hooks.PRE_HANDOVER, fn)
    assert hooks.registered_events() == ()            # event dropped → clean introspection


def test_unregister_absent_is_a_noop():
    fn = lambda c: None
    hooks.unregister_hook(hooks.PRE_HANDOVER, fn)      # never registered → no-op, no raise
    hooks.register_hook(hooks.PRE_HANDOVER, fn)
    hooks.unregister_hook(hooks.PRE_HANDOVER, lambda c: None)  # a DIFFERENT fn → no-op (identity)
    assert hooks.hook_count(hooks.PRE_HANDOVER) == 1


def test_unregister_is_idempotent_for_apply_pattern():
    # the _apply_*-style enable/disable cycle: register on enable, unregister on disable, repeat.
    fn = lambda c: None
    hooks.register_hook(hooks.PRE_HANDOVER, fn)
    hooks.register_hook(hooks.PRE_HANDOVER, fn)        # dedup → still 1
    hooks.unregister_hook(hooks.PRE_HANDOVER, fn)
    hooks.unregister_hook(hooks.PRE_HANDOVER, fn)      # already gone → no-op
    hooks.register_hook(hooks.PRE_HANDOVER, fn)        # re-enable
    assert hooks.hook_count(hooks.PRE_HANDOVER) == 1
