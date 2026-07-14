"""Quality Circuit Breaker — per-task output-quality trend (ACK, #602 S602-9).

Proves, offline:

  * `QualityBreaker` trips on `min_consecutive` sub-threshold scores, recovers on an at/above-threshold score,
    is fail-open-safe (never raises; a hiccup leaves it untripped), and exposes a snapshot consumed by the
    engine's protected-boundary hold;
  * the engine wiring is always-on: `gx10._apply_quality_breaker` always builds the SEPARATE
    `_QUALITY_BREAKER`, and the retired enable leaf cannot clear it.

    python -m pytest ack/tests/test_quality.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ack.quality import QualityBreaker, QualitySnapshot

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))


# ─── pure breaker ───────────────────────────────────────────────────────────────────────────────────
def test_trips_after_min_consecutive_low():
    b = QualityBreaker(threshold=0.5, min_consecutive=3)
    assert b.record(0.2) is False
    assert b.record(0.1) is False
    assert b.record(0.4) is True            # 3rd consecutive low → tripped
    assert b.tripped is True


def test_high_score_resets_the_streak():
    b = QualityBreaker(threshold=0.5, min_consecutive=3)
    b.record(0.1)
    b.record(0.2)
    assert b.record(0.9) is False           # recovery resets the streak
    assert b.record(0.1) is False           # streak restarts at 1
    assert b.record(0.2) is False           # 2 < 3
    assert b.tripped is False


def test_at_threshold_is_not_low():
    b = QualityBreaker(threshold=0.5, min_consecutive=2)
    assert b.record(0.5) is False           # 0.5 is NOT < 0.5
    assert b.record(0.5) is False
    assert b.tripped is False


def test_reset_clears_trip_and_streak():
    b = QualityBreaker(threshold=0.5, min_consecutive=2)
    b.record(0.1); b.record(0.1)
    assert b.tripped is True
    b.reset()
    assert b.tripped is False
    assert b.record(0.1) is False           # streak restarted post-reset


def test_score_is_clamped():
    b = QualityBreaker(threshold=0.5, min_consecutive=1)
    assert b.record(5.0) is False           # clamps to 1.0 → not low
    assert b.record(-3.0) is True           # clamps to 0.0 → low → trips (min_consecutive 1)


def test_garbage_score_does_not_trip_fail_open():
    """A broken score is a hiccup, NOT degradation — it must be skipped (no record, no trip), never raise."""
    b = QualityBreaker(threshold=0.5, min_consecutive=1)
    for bad in ("not-a-number", None, float("nan"), float("inf"), float("-inf"), object()):
        assert b.record(bad) is False       # skipped, never tripped on garbage
    assert b.tripped is False
    assert b.snapshot().samples == 0        # nothing was recorded


def test_garbage_score_does_not_break_a_real_streak():
    b = QualityBreaker(threshold=0.5, min_consecutive=2)
    assert b.record(0.1) is False           # 1 low
    assert b.record("garbage") is False     # skipped — does NOT reset the streak, does NOT advance it
    assert b.record(0.1) is True            # 2nd real low → trips


def test_constructor_never_raises_on_garbage_params():
    b = QualityBreaker(threshold="x", min_consecutive="y", window=None)   # all garbage
    # garbage threshold → 0.0 → a score is never "< 0.0" → fail-open: never trips.
    for _ in range(10):
        b.record(0.0)
    assert b.tripped is False


def test_non_finite_threshold_is_fail_open():
    """A non-finite threshold (inf/nan) must be fail-open (→ 0.0, never trips), NOT clamp to 1.0 (trip-all)."""
    for bad in (float("inf"), float("-inf"), float("nan")):
        b = QualityBreaker(threshold=bad, min_consecutive=1)
        for _ in range(5):
            b.record(0.0)
        assert b.tripped is False


def test_hostile_float_score_is_skipped():
    class _Bad:
        def __float__(self):
            raise RuntimeError("nope")
    b = QualityBreaker(threshold=0.5, min_consecutive=1)
    assert b.record(_Bad()) is False        # raising __float__ → skipped, not a trip
    assert b.snapshot().samples == 0


def test_oversized_window_never_raises():
    b = QualityBreaker(window=10 ** 1000)    # deque(maxlen=huge) would OverflowError → must be capped
    b.record(0.9)
    assert b.snapshot().samples == 1


def test_snapshot_shape():
    b = QualityBreaker(threshold=0.5, min_consecutive=2)
    b.record(0.1)
    snap = b.snapshot()
    assert isinstance(snap, QualitySnapshot)
    assert snap.tripped is False and snap.consecutive_low == 1 and snap.samples == 1
    b.record(0.1)
    assert b.snapshot().tripped is True and "degraded" in b.snapshot().reason


def test_window_caps_retained_samples():
    b = QualityBreaker(threshold=0.5, min_consecutive=99, window=3)
    for _ in range(10):
        b.record(0.9)
    assert b.snapshot().samples == 3        # only the window is retained


def test_reconfigure_preserves_recovered_state_without_replaying_an_old_low_run():
    b = QualityBreaker(threshold=0.5, min_consecutive=3, window=20)
    for score in (0.1, 0.1, 0.1):
        b.record(score)
    b.record(0.9)
    b.reset()                               # engine recovery keeps history but clears the live latch

    rebuilt = b.reconfigure(threshold=0.5, min_consecutive=3, window=10)

    assert list(rebuilt._scores) == [0.1, 0.1, 0.1, 0.9]
    assert rebuilt.snapshot().consecutive_low == 0
    assert rebuilt.tripped is False


def test_reconfigure_preserves_a_live_latch_even_when_new_rule_is_looser():
    b = QualityBreaker(threshold=0.5, min_consecutive=2)
    b.record(0.1); b.record(0.1)
    assert b.tripped is True

    rebuilt = b.reconfigure(threshold=0.5, min_consecutive=5, window=20)

    assert rebuilt.snapshot().consecutive_low == 2
    assert rebuilt.tripped is True


def test_reconfigure_can_trip_from_a_newly_revealed_trailing_low_streak():
    b = QualityBreaker(threshold=0.5, min_consecutive=3)
    for score in (0.9, 0.6, 0.7, 0.75):
        b.record(score)
    assert b.tripped is False

    rebuilt = b.reconfigure(threshold=0.8, min_consecutive=3, window=20)

    assert rebuilt.snapshot().consecutive_low == 3
    assert rebuilt.tripped is True


def test_snapshot_is_frozen():
    snap = QualityBreaker().snapshot()
    with pytest.raises(Exception):
        snap.tripped = True


# ─── engine wiring — always-on, separate from the availability breaker ──────────────────────────────
@pytest.fixture
def _clean_breaker():
    import gx10
    saved = gx10._QUALITY_BREAKER
    saved_tripped = gx10._QUALITY_TRIPPED
    gx10._QUALITY_BREAKER = None
    gx10._QUALITY_TRIPPED = None
    yield gx10
    gx10._QUALITY_BREAKER = saved
    gx10._QUALITY_TRIPPED = saved_tripped


def test_apply_builds_breaker_with_tuning(_clean_breaker):
    gx10 = _clean_breaker
    gx10._apply_quality_breaker({"quality": {"threshold": 0.5, "min_consecutive": 2, "window": 5}})
    assert isinstance(gx10._quality_breaker(), QualityBreaker)


def test_apply_default_config_builds_breaker(_clean_breaker):
    gx10 = _clean_breaker
    gx10._apply_quality_breaker(gx10._code_defaults())
    assert isinstance(gx10._quality_breaker(), QualityBreaker)


def test_legacy_false_cannot_clear_the_breaker(_clean_breaker):
    gx10 = _clean_breaker
    gx10._apply_quality_breaker({"quality": {}})
    existing = gx10._quality_breaker()
    gx10._apply_quality_breaker({"quality": {"enabled": False}})
    assert gx10._quality_breaker() is existing


def test_runtime_quality_tuning_rebuilds_recomputes_and_preserves_compatible_history(
        _clean_breaker, monkeypatch):
    gx10 = _clean_breaker
    cfg = gx10._code_defaults()
    gx10._apply_quality_breaker(cfg)
    original = gx10._quality_breaker()
    for score in (0.6, 0.7, 0.75):
        original.record(score)
    assert original.snapshot().samples == 3 and original.tripped is False

    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", cfg)
    monkeypatch.setattr(gx10, "_ui_print", lambda *a, **k: None)
    gx10._dispatch(None, "config set quality.threshold 0.8")
    threshold_tuned = gx10._quality_breaker()
    assert threshold_tuned is not original
    assert threshold_tuned.snapshot().samples == 3
    assert threshold_tuned.snapshot().threshold == 0.8
    assert threshold_tuned.tripped is True and gx10._quality_tripped() is not None

    gx10._dispatch(None, "config set quality.min_consecutive 4")
    assert gx10._quality_breaker().snapshot().samples == 3
    assert gx10._quality_breaker().snapshot().min_consecutive == 4
    assert gx10._quality_breaker().tripped is True and gx10._quality_tripped() is not None

    gx10._dispatch(None, "config set quality.min_consecutive 2")
    gx10._dispatch(None, "config set quality.window 2")
    final = gx10._quality_breaker()
    assert final.snapshot().samples == 2
    assert getattr(final, "_scores").maxlen == 2
    assert list(getattr(final, "_scores")) == [0.7, 0.75]
    assert final.tripped is True and gx10._quality_tripped() is not None


def test_runtime_window_tuning_does_not_resurrect_a_recovered_trip(_clean_breaker, monkeypatch):
    gx10 = _clean_breaker
    cfg = gx10._code_defaults()
    cfg["quality"].update({"threshold": 0.5, "min_consecutive": 3, "window": 20})
    gx10._apply_quality_breaker(cfg)
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", cfg)
    monkeypatch.setattr(gx10, "_ui_print", lambda *a, **k: None)

    for score in (0.1, 0.1, 0.1):
        gx10._set_last_verdict(SimpleNamespace(score=score))
        gx10._quality_consumer_hook({})
    assert gx10._quality_tripped() is not None

    gx10._set_last_verdict(SimpleNamespace(score=0.9))
    gx10._quality_consumer_hook({})
    assert gx10._quality_tripped() is None
    assert gx10._quality_breaker().tripped is False

    gx10._dispatch(None, "config set quality.window 7")
    tuned = gx10._quality_breaker()
    assert list(tuned._scores) == [0.1, 0.1, 0.1, 0.9]
    assert tuned.snapshot().consecutive_low == 0
    assert tuned.tripped is False and gx10._quality_tripped() is None

    for score in (0.1, 0.1, 0.1):
        gx10._set_last_verdict(SimpleNamespace(score=score))
        gx10._quality_consumer_hook({})
    assert gx10._quality_breaker().tripped is True
    assert gx10._quality_tripped() is not None


def test_apply_is_separate_from_availability_breaker(_clean_breaker):
    gx10 = _clean_breaker
    before = dict(gx10._CODE_AGENT_BREAKER)
    gx10._apply_quality_breaker({"quality": {}})
    # the quality breaker is its own object; the availability breaker dict is untouched (byte-for-byte).
    assert gx10._quality_breaker() is not gx10._CODE_AGENT_BREAKER
    assert dict(gx10._CODE_AGENT_BREAKER) == before


def test_snapshot_reason_after_trip_then_recovery_reports_the_rule_not_zero_streak():
    # a trip is latched until reset(); a later recovery score zeroes the LIVE streak but not the trip, so
    # the snapshot reason must report the trip RULE — regression for the nonsensical "0 consecutive ... < x".
    b = QualityBreaker(threshold=0.5, min_consecutive=3)
    b.record(0.1); b.record(0.1); b.record(0.1)     # 3 consecutive low → tripped
    assert b.record(0.9) is True                    # still tripped (latched); the live streak resets to 0
    snap = b.snapshot()
    assert snap.tripped is True and snap.consecutive_low == 0
    assert "0 consecutive" not in snap.reason
    assert "3+ consecutive" in snap.reason and "0.50" in snap.reason
