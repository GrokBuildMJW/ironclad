"""#1060 (epic #1059): runtime self-telemetry — a bounded rolling record of per-generation latency, token
cost and errors + an SLO/anomaly check, exposed at GET /metrics so an unattended deployment is observable."""
from __future__ import annotations

import sys
import time
import types
from pathlib import Path

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=object))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import telemetry as tel  # noqa: E402


def test_snapshot_aggregates_turns_errors_latency_and_cost():
    t = tel.Telemetry(max_samples=100)
    for i, (lat, ok) in enumerate([(1.0, True), (3.0, True), (2.0, False), (10.0, True)]):
        t.record_turn(latency_s=lat, prompt_tokens=100, completion_tokens=50, ok=ok, ts=100.0 + i)
    snap = t.snapshot(now=200.0)
    assert snap["turns"] == 4 and snap["errors"] == 1 and abs(snap["error_rate"] - 0.25) < 1e-9
    assert snap["prompt_tokens"] == 400 and snap["completion_tokens"] == 200 and snap["total_tokens"] == 600
    assert snap["latency_p95_s"] >= snap["latency_p50_s"] > 0


def test_window_filters_out_old_samples():
    t = tel.Telemetry()
    t.record_turn(latency_s=1.0, prompt_tokens=1, completion_tokens=1, ok=True, ts=100.0)     # old
    t.record_turn(latency_s=2.0, prompt_tokens=1, completion_tokens=1, ok=True, ts=1000.0)    # recent
    snap = t.snapshot(now=1000.0, window_s=100.0)                                             # only ts >= 900
    assert snap["turns"] == 1 and snap["latency_avg_s"] == 2.0


def test_slo_ok_without_traffic_and_flags_breaches():
    assert tel.slo_status({"turns": 0})["ok"] is True
    breached = tel.slo_status({"turns": 10, "error_rate": 0.5, "latency_p95_s": 120.0},
                              max_error_rate=0.2, max_p95_latency_s=60.0)
    assert breached["ok"] is False and len(breached["breaches"]) == 2
    assert tel.slo_status({"turns": 10, "error_rate": 0.05, "latency_p95_s": 10.0})["ok"] is True


def test_anomaly_detects_spike_but_needs_enough_traffic():
    window = {"turns": 30, "error_rate": 0.4, "latency_p95_s": 20.0}
    baseline = {"turns": 100, "error_rate": 0.1, "latency_p95_s": 20.0}
    a = tel.anomaly(window, baseline)
    assert a["anomaly"] is True and any("error_rate" in s for s in a["signals"])
    thin = tel.anomaly({"turns": 5, "error_rate": 0.9, "latency_p95_s": 99.0},
                       {"turns": 5, "error_rate": 0.1, "latency_p95_s": 1.0})
    assert thin["anomaly"] is False                                                          # too few → no false alarm


def test_rolling_window_is_bounded():
    t = tel.Telemetry(max_samples=3)
    for i in range(10):
        t.record_turn(latency_s=1.0, prompt_tokens=1, completion_tokens=1, ok=True, ts=float(i))
    assert t.snapshot(now=100.0)["turns"] == 3                                                # only the last 3 kept


def test_metrics_report_has_the_endpoint_shape():
    import gx10
    tel.reset()
    tel.record_turn(latency_s=2.0, prompt_tokens=100, completion_tokens=50, ok=True, ts=time.time())
    rep = gx10._metrics_report()
    assert {"all_time", "window", "window_s", "slo", "anomaly"} <= set(rep)
    assert rep["all_time"]["turns"] >= 1 and rep["slo"]["ok"] in (True, False)
    tel.reset()                                                                              # clean the global collector
