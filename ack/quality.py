"""Quality Circuit Breaker — per-task output-quality trend (Agent-Contract-Kernel, #602 S602-9).

> **A breaker for output QUALITY, separate from the availability breaker.** The engine already trips a
> per-peer *availability* breaker (`_CODE_AGENT_BREAKER`) that drives code-agent failover. This is a
> **distinct, agent-agnostic** breaker for *output quality*: it watches the trend of MARK-ONLY
> :class:`~ack.verify.VerdictResult` scores and trips on **sustained degradation** — so the engine can
> escalate / surface to the operator instead of silently churning out low-quality work.

:class:`QualityBreaker` is a pure trend recorder: it never performs mutations or raises. The engine consumes
its latched trip as a pre-write staging hold, while other callers may use the snapshot for observability.
Pure (in-memory, no transport/model/I/O), snapshot-testable; imports only the stdlib.

Trip rule: ``min_consecutive`` scores in a row below ``threshold`` (a sustained downward trend) → tripped;
an at/above-threshold score resets the streak. A trip stays **latched until** :meth:`QualityBreaker.reset`
(a recovery score clears the streak but not the trip), so :meth:`QualityBreaker.snapshot` reports the trip
rule rather than the live streak. The score is the verifier's, clamped to [0, 1].
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Any


#: A sane upper bound on the score window (a deque maxlen must fit Py_ssize_t — a huge config value would
#: otherwise raise OverflowError; capping keeps construction fail-open-safe).
_MAX_WINDOW = 100_000


def _clamp01(x: Any) -> float:
    """Coerce *x* to a float in [0, 1] for a THRESHOLD; never raises (bad input → 0.0, i.e. a never-trip
    fail-open threshold)."""
    try:
        f = float(x)
    except Exception:   # noqa: BLE001
        return 0.0
    if not math.isfinite(f):   # NaN / ±inf → 0.0 (a never-trip fail-open threshold), not a clamp to 1.0
        return 0.0
    return 0.0 if f < 0.0 else 1.0 if f > 1.0 else f


def _coerce_score(x: Any) -> "float | None":
    """Coerce a recorded SCORE to a float in [0, 1], or ``None`` when it is not a valid finite number.

    Distinct from :func:`_clamp01`: a garbage / NaN / infinite SCORE returns ``None`` so :meth:`record` can
    SKIP it — a broken score is a hiccup, not evidence of degradation, so it must never trip the breaker
    (fail-open-safe). A valid out-of-range score is clamped (e.g. -3 → 0.0, 5 → 1.0)."""
    try:
        f = float(x)
    except Exception:   # noqa: BLE001
        return None
    if not math.isfinite(f):   # NaN / ±inf → not a valid score → skip (fail-open)
        return None
    return 0.0 if f < 0.0 else 1.0 if f > 1.0 else f


def _safe_int(x: Any, default: int) -> int:
    try:
        return int(x)
    except Exception:   # noqa: BLE001
        return default


def _safe_window(x: Any, default: int = 20) -> int:
    """A deque maxlen in [1, _MAX_WINDOW] — never raises, never overflows ``deque(maxlen=…)``."""
    n = _safe_int(x, default)
    if n < 1:
        return default
    return min(n, _MAX_WINDOW)


@dataclass(frozen=True)
class QualitySnapshot:
    """An immutable view of the breaker's state — for surfacing / observability (never gates)."""

    tripped: bool
    consecutive_low: int
    samples: int
    threshold: float
    min_consecutive: int
    reason: str


class QualityBreaker:
    """A SEPARATE, agent-agnostic, per-task output-QUALITY breaker (#602 SUB-9). NOT an extension of the
    availability breaker. Feed it verifier scores via :meth:`record`; read :attr:`tripped` /
    :meth:`snapshot` to consume sustained degradation. The recorder itself is fail-open-safe (never raises;
    a hiccup leaves it untripped); the engine applies the protected-boundary hold."""

    def __init__(self, *, threshold: float = 0.5, min_consecutive: int = 3, window: int = 20) -> None:
        # A garbage threshold → 0.0 (a score is never "< 0.0" → the breaker simply never trips: fail-open).
        try:
            self._threshold = _clamp01(threshold)
            self._min = max(1, _safe_int(min_consecutive, 3))
            self._scores: "deque[float]" = deque(maxlen=_safe_window(window))
        except Exception:   # noqa: BLE001 — construction is fail-open-safe: any hiccup → sane defaults
            self._threshold, self._min, self._scores = 0.5, 3, deque(maxlen=20)
        self._consecutive_low = 0
        self._tripped = False

    def record(self, score: Any) -> bool:
        """Record a quality *score* (clamped to [0, 1]). A run of ``min_consecutive`` sub-threshold scores
        trips the breaker; an at/above-threshold score resets the streak. Returns the (post-record) tripped
        state. **Never raises** — a hiccup is swallowed and leaves the state unchanged (fail-open-safe)."""
        try:
            s = _coerce_score(score)
            if s is None:
                return self._tripped   # a garbage/NaN/inf score is a hiccup, NOT degradation → skip, unchanged
            self._scores.append(s)
            if s < self._threshold:
                self._consecutive_low += 1
            else:
                self._consecutive_low = 0
            if self._consecutive_low >= self._min:
                self._tripped = True
        except Exception:   # noqa: BLE001 — fail-open-safe: a record hiccup never trips and never raises
            pass
        return self._tripped

    def reconfigure(self, *, threshold: float, min_consecutive: int, window: int) -> "QualityBreaker":
        """Return a breaker with new tuning while preserving bounded history and the live latch state.

        Only the retained history's *trailing* low-score streak is relevant after a rebuild: an earlier run
        may already have been acknowledged by a later recovery score. An existing latch remains latched;
        otherwise the rebuilt breaker trips only when the trailing streak satisfies the new rule. Never
        raises — a rebuild hiccup returns a safely constructed breaker with the previous live latch state.
        """
        rebuilt = QualityBreaker(threshold=threshold, min_consecutive=min_consecutive, window=window)
        try:
            new_window = rebuilt._scores.maxlen
            history = list(self._scores)
            if isinstance(new_window, int):
                history = history[-new_window:]
            rebuilt._scores.extend(history)

            trailing_low = 0
            for score in reversed(history):
                if score >= rebuilt._threshold:
                    break
                trailing_low += 1
            rebuilt._consecutive_low = trailing_low
            rebuilt._tripped = self._tripped or trailing_low >= rebuilt._min
        except Exception:   # noqa: BLE001 — live reconfiguration is fail-open-safe and never raises
            rebuilt._tripped = self._tripped
        return rebuilt

    @property
    def tripped(self) -> bool:
        return self._tripped

    def reset(self) -> None:
        """Clear the trip + the streak (e.g. after the operator acknowledges / a recovery). Never raises."""
        self._consecutive_low = 0
        self._tripped = False

    def snapshot(self) -> QualitySnapshot:
        """An immutable view of the current state (for surfacing). Never raises."""
        try:
            samples = len(self._scores)
            # A trip is latched until reset(), so the LIVE streak may already be 0 after a recovery score —
            # report the trip RULE (min_consecutive), not the live streak, to avoid a "0 consecutive" reason.
            reason = (
                f"quality degraded: tripped on {self._min}+ consecutive score(s) < {self._threshold:.2f}"
                if self._tripped else
                f"ok: {self._consecutive_low}/{self._min} low, {samples} sample(s)"
            )
            return QualitySnapshot(self._tripped, self._consecutive_low, samples,
                                   self._threshold, self._min, reason)
        except Exception:   # noqa: BLE001 — surfacing must never raise
            return QualitySnapshot(self._tripped, 0, 0, self._threshold, self._min, "")
