"""#1060: runtime self-telemetry.

A process-global, thread-safe, BOUNDED rolling record of per-generation latency, token cost, and errors,
plus an SLO / anomaly check — so a running (unattended) deployment is observable via ``GET /metrics`` and a
degradation is detectable without a human watching the console. stdlib-only; the clock is injected (the
caller passes ``ts`` / ``now``) so aggregation is deterministic in tests.
"""
from __future__ import annotations

import threading
from collections import deque
from typing import Any, Dict, List, Optional

_MAX_SAMPLES = 2000          # rolling window cap — can't grow unbounded on an always-on box


def _pct(sorted_vals: "List[float]", p: float) -> float:
    """Linear-interpolated percentile of an already-sorted list (empty → 0.0)."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


class Telemetry:
    def __init__(self, max_samples: int = _MAX_SAMPLES):
        self._lock = threading.Lock()
        self._samples: "deque" = deque(maxlen=max_samples)   # {ts, latency, prompt, completion, ok}
        self.started_ts: "Optional[float]" = None

    def record_turn(self, *, latency_s: float, prompt_tokens: int, completion_tokens: int,
                    ok: bool, ts: float) -> None:
        with self._lock:
            if self.started_ts is None:
                self.started_ts = ts
            self._samples.append({"ts": float(ts), "latency": float(latency_s or 0.0),
                                  "prompt": int(prompt_tokens or 0), "completion": int(completion_tokens or 0),
                                  "ok": bool(ok)})

    def _agg(self, samples: "List[Dict[str, Any]]") -> "Dict[str, Any]":
        n = len(samples)
        if n == 0:
            return {"turns": 0, "errors": 0, "error_rate": 0.0, "latency_avg_s": 0.0,
                    "latency_p50_s": 0.0, "latency_p95_s": 0.0,
                    "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        lat = sorted(s["latency"] for s in samples)
        errors = sum(1 for s in samples if not s["ok"])
        pt = sum(s["prompt"] for s in samples)
        ct = sum(s["completion"] for s in samples)
        return {"turns": n, "errors": errors, "error_rate": errors / n,
                "latency_avg_s": sum(lat) / n, "latency_p50_s": _pct(lat, 50), "latency_p95_s": _pct(lat, 95),
                "prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct}

    def snapshot(self, *, now: float, window_s: "Optional[float]" = None) -> "Dict[str, Any]":
        """Aggregate all samples (``window_s=None``) or only those within the last *window_s* seconds."""
        with self._lock:
            samples = list(self._samples)
        if window_s is not None:
            cutoff = now - window_s
            samples = [s for s in samples if s["ts"] >= cutoff]
        return self._agg(samples)

    def reset(self) -> None:
        with self._lock:
            self._samples.clear()
            self.started_ts = None


def slo_status(snap: "Dict[str, Any]", *, max_error_rate: float = 0.2,
               max_p95_latency_s: float = 60.0) -> "Dict[str, Any]":
    """Evaluate the SLOs against a snapshot. Returns ``{ok, breaches}`` — no breaches (and ok=True) when
    there is no traffic yet."""
    breaches: "List[str]" = []
    if snap.get("turns", 0) > 0:
        if snap["error_rate"] > max_error_rate:
            breaches.append(f"error_rate {snap['error_rate']:.0%} > {max_error_rate:.0%}")
        if snap["latency_p95_s"] > max_p95_latency_s:
            breaches.append(f"p95_latency {snap['latency_p95_s']:.1f}s > {max_p95_latency_s:.0f}s")
    return {"ok": not breaches, "breaches": breaches}


def anomaly(window: "Dict[str, Any]", baseline: "Dict[str, Any]", *, factor: float = 2.0,
            min_turns: int = 20) -> "Dict[str, Any]":
    """A cheap recent-vs-baseline anomaly signal: the recent *window* error_rate or p95 latency exceeding the
    all-time *baseline* by *factor* (only once enough traffic exists). Returns ``{anomaly, signals}``."""
    signals: "List[str]" = []
    if window.get("turns", 0) >= min_turns and baseline.get("turns", 0) >= min_turns:
        if baseline["error_rate"] > 0 and window["error_rate"] > factor * baseline["error_rate"]:
            signals.append(f"error_rate spike: {window['error_rate']:.0%} vs baseline {baseline['error_rate']:.0%}")
        if baseline["latency_p95_s"] > 0 and window["latency_p95_s"] > factor * baseline["latency_p95_s"]:
            signals.append(f"latency spike: p95 {window['latency_p95_s']:.1f}s vs baseline {baseline['latency_p95_s']:.1f}s")
    return {"anomaly": bool(signals), "signals": signals}


#: The process-global collector + thin module-level convenience wrappers.
_T = Telemetry()


def record_turn(**kw) -> None:
    _T.record_turn(**kw)


def snapshot(*, now: float, window_s: "Optional[float]" = None) -> "Dict[str, Any]":
    return _T.snapshot(now=now, window_s=window_s)


def reset() -> None:
    _T.reset()
