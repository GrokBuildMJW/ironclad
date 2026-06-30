"""Loop Profiles — the pure per-TaskType resolver + the engine wiring (ACK, #602 S602-8a).

Proves, offline:

  * `resolve_loop_profile` deep-merges code defaults ← `default` ← `by_type[<type>]`, applies only present
    keys, clamps retry_budget to the hard ceiling, is deterministic, and NEVER raises;
  * with NO loop_profiles configured the resolved profile equals the engine globals (byte-identical);
  * the engine accessor `gx10._loop_profile` and the chat-loop bound stay byte-identical by default and pick
    up an override.

    python -m pytest ack/tests/test_loop_profile.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from ack.loop_profile import LoopProfile, resolve_loop_profile

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

DEFAULTS = dict(default_max_iterations=20, default_retry_budget=3, max_retry_budget=3)


# ─── pure resolver ──────────────────────────────────────────────────────────────────────────────────
def test_empty_profiles_fall_back_to_globals():
    p = resolve_loop_profile({}, "research", **DEFAULTS)
    assert (p.max_iterations, p.retry_budget, p.effort) == (20, 3, "medium")


def test_none_profiles_fall_back_to_globals():
    p = resolve_loop_profile(None, None, **DEFAULTS)
    assert p.max_iterations == 20


def test_default_layer_overrides_globals():
    p = resolve_loop_profile({"default": {"max_iterations": 30}}, "feature", **DEFAULTS)
    assert p.max_iterations == 30
    assert p.retry_budget == 3            # untouched key keeps the global


def test_by_type_overrides_default_layer():
    profiles = {"default": {"max_iterations": 30}, "by_type": {"research": {"max_iterations": 40}}}
    assert resolve_loop_profile(profiles, "research", **DEFAULTS).max_iterations == 40   # by_type wins
    assert resolve_loop_profile(profiles, "feature", **DEFAULTS).max_iterations == 30    # falls to default


def test_partial_by_type_layers_cleanly():
    profiles = {"default": {"max_iterations": 30, "effort": "high"},
                "by_type": {"research": {"max_iterations": 40}}}
    p = resolve_loop_profile(profiles, "research", **DEFAULTS)
    assert (p.max_iterations, p.effort) == (40, "high")   # mi from by_type, effort from default


def test_retry_budget_is_clamped_to_ceiling():
    p = resolve_loop_profile({"default": {"retry_budget": 99}}, None, **DEFAULTS)
    assert p.retry_budget == 3            # clamped to max_retry_budget


def test_retry_budget_floor_is_one():
    p = resolve_loop_profile({"default": {"retry_budget": 0}}, None, **DEFAULTS)
    assert p.retry_budget == 1


def test_max_iterations_floor_is_one():
    p = resolve_loop_profile({"default": {"max_iterations": 0}}, None, **DEFAULTS)
    assert p.max_iterations == 1          # a CONFIGURED 0 is floored to 1


def test_fallback_max_iterations_passes_through_verbatim():
    """The engine FALLBACK is NOT floored — an existing deployment with max_iterations 0 keeps today's
    zero-iteration behaviour (byte-identical); only an operator-supplied override is floored."""
    p = resolve_loop_profile({}, None, default_max_iterations=0, default_retry_budget=3, max_retry_budget=3)
    assert p.max_iterations == 0


def test_enum_like_task_type_uses_value():
    class _T:
        value = "research"
    profiles = {"by_type": {"research": {"max_iterations": 40}}}
    assert resolve_loop_profile(profiles, _T(), **DEFAULTS).max_iterations == 40


def test_none_key_present_in_by_type_is_not_matched_for_none_type():
    # task_type None must not match a by_type entry → falls back to default/global.
    profiles = {"by_type": {"None": {"max_iterations": 99}, "none": {"max_iterations": 98}}}
    assert resolve_loop_profile(profiles, None, **DEFAULTS).max_iterations == 20


def test_resolver_never_raises_on_garbage():
    assert resolve_loop_profile("not-a-dict", object(), **DEFAULTS).max_iterations == 20
    assert resolve_loop_profile({"default": "nope", "by_type": 5}, [], **DEFAULTS).max_iterations == 20
    assert resolve_loop_profile({"default": {"max_iterations": "x"}}, None, **DEFAULTS).max_iterations == 20


def test_returns_loopprofile_instance():
    assert isinstance(resolve_loop_profile({}, None, **DEFAULTS), LoopProfile)


# ─── 8b: per-profile eval-verifier activation ───────────────────────────────────────────────────────
def test_eval_verifiers_empty_by_default():
    assert resolve_loop_profile({}, "research", **DEFAULTS).eval_verifiers == ()


def test_eval_verifiers_from_default_layer():
    p = resolve_loop_profile({"default": {"eval": ["rules", "grounding"]}}, "feature", **DEFAULTS)
    assert p.eval_verifiers == ("rules", "grounding")


def test_eval_verifiers_by_type_overrides_default():
    profiles = {"default": {"eval": ["rules"]}, "by_type": {"security": {"eval": ["rules", "judge"]}}}
    assert resolve_loop_profile(profiles, "security", **DEFAULTS).eval_verifiers == ("rules", "judge")
    assert resolve_loop_profile(profiles, "feature", **DEFAULTS).eval_verifiers == ("rules",)


def test_eval_verifiers_coerces_to_str_tuple():
    p = resolve_loop_profile({"default": {"eval": ["rules", 5, None, "judge"]}}, None, **DEFAULTS)
    assert p.eval_verifiers == ("rules", "judge")        # non-str entries dropped


def test_eval_verifiers_garbage_is_empty():
    assert resolve_loop_profile({"default": {"eval": "notalist"}}, None, **DEFAULTS).eval_verifiers == ()


# ─── engine accessor + chat-loop bound ──────────────────────────────────────────────────────────────
def test_engine_accessor_byte_identical_default(monkeypatch):
    import gx10
    # the shipped default config has an empty loop_profiles → the accessor returns the global MAX_ITERATIONS.
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", gx10._code_defaults(), raising=False)
    assert gx10._loop_profile().max_iterations == gx10.MAX_ITERATIONS


def test_engine_accessor_picks_up_override(monkeypatch):
    import gx10
    cfg = gx10._code_defaults()
    cfg["loop_profiles"] = {"default": {"max_iterations": gx10.MAX_ITERATIONS + 7}, "by_type": {}}
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", cfg, raising=False)
    assert gx10._loop_profile().max_iterations == gx10.MAX_ITERATIONS + 7


def test_engine_accessor_never_raises_without_config(monkeypatch):
    import gx10
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", None, raising=False)
    # falls back to _code_defaults() internally → still the global, no raise.
    assert gx10._loop_profile().max_iterations == gx10.MAX_ITERATIONS


def test_engine_accessor_byte_identical_with_zero_max_iter(monkeypatch):
    """Even an unusual global (MAX_ITERATIONS=0) is preserved by the default profile (byte-identical)."""
    import gx10
    monkeypatch.setattr(gx10, "MAX_ITERATIONS", 0, raising=False)
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", gx10._code_defaults(), raising=False)
    assert gx10._loop_profile().max_iterations == 0


def test_run_loop_bound_uses_the_profile():
    """Wiring guard: the chat loop reads the profile, not the bare global (fails if run() regresses)."""
    import inspect
    import gx10
    src = inspect.getsource(gx10.GX10.run)
    assert "_loop_profile()" in src
    assert "range(MAX_ITERATIONS)" not in src
