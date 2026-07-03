"""#1061 (epic #1059): alerting pipeline — turn the telemetry SLO/anomaly (#1060) + inbound external alerts
into paged notifications via the #1083 webhook. Pure rules (evaluate/format/normalize) + the engine wiring
(_alert_scan self-scan, _receive_alert inbound, _notify_alert transport). Default-off; deploy-correlated."""
from __future__ import annotations

import sys
import types
from pathlib import Path

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=object))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import alerting  # noqa: E402


def test_evaluate_derives_slo_and_anomaly_alerts_deploy_correlated():
    report = {"slo": {"ok": False, "breaches": ["error_rate 50% > 20%"]},
              "anomaly": {"anomaly": True, "signals": ["latency spike: p95 20s vs 5s"]}}
    alerts = alerting.evaluate(report, version="abc123")
    sev = {a["kind"]: a["severity"] for a in alerts}
    assert sev == {"slo_breach": "critical", "anomaly": "warning"}
    assert all("abc123" in a["message"] for a in alerts)          # correlated with the running deploy


def test_evaluate_all_green_and_none_are_empty():
    assert alerting.evaluate({"slo": {"ok": True, "breaches": []}, "anomaly": {"signals": []}}) == []
    assert alerting.evaluate(None) == []


def test_format_alert_line():
    assert alerting.format_alert({"severity": "critical", "message": "boom"}) == "[Ironclad][CRITICAL] boom"


def test_normalize_inbound_validates_and_clamps():
    a, err = alerting.normalize_inbound({"message": "disk full", "severity": "CRITICAL", "source": "nagios"})
    assert err is None and a["severity"] == "critical" and a["source"] == "nagios"
    assert alerting.normalize_inbound({"message": "  "})[1]        # empty message → error
    assert alerting.normalize_inbound("not a dict")[1]             # non-object → error
    assert alerting.normalize_inbound({"message": "x", "severity": "bogus"})[0]["severity"] == "warning"


def test_receive_alert_normalizes_and_pages(monkeypatch):
    import gx10
    monkeypatch.setattr(gx10, "orchestrator_version", lambda: "v9")
    sent = []
    monkeypatch.setattr(gx10, "_notify_alert", lambda a: sent.append(a) or True)
    r = gx10._receive_alert({"message": "external boom", "severity": "critical"})
    assert r == {"ok": True, "notified": True, "severity": "critical"}
    assert sent and sent[0]["version"] == "v9"


def test_receive_alert_rejects_malformed():
    import gx10
    assert gx10._receive_alert({"message": ""})["ok"] is False
    assert gx10._receive_alert("nope")["ok"] is False


def test_alert_scan_pages_each_rule(monkeypatch):
    import gx10
    monkeypatch.setattr(gx10, "_metrics_report",
                        lambda: {"slo": {"breaches": ["b1"]}, "anomaly": {"signals": []}})
    monkeypatch.setattr(gx10, "orchestrator_version", lambda: "v1")
    paged = []
    monkeypatch.setattr(gx10, "_notify_alert", lambda a: paged.append(a) or True)
    alerts = gx10._alert_scan()
    assert len(alerts) == 1 and paged[0]["kind"] == "slo_breach"


def test_notify_alert_gated_on_a_configured_webhook(monkeypatch):
    import gx10
    monkeypatch.setattr(gx10, "NOTIFY_WEBHOOK", "")
    assert gx10._notify_alert({"message": "x", "severity": "warning"}) is False   # no webhook → no-op
    import notify
    monkeypatch.setattr(gx10, "NOTIFY_WEBHOOK", "https://hooks/x")
    cap = {}
    monkeypatch.setattr(notify, "notify_webhook", lambda url, text, **k: cap.update(url=url, text=text) or True)
    assert gx10._notify_alert({"message": "boom", "severity": "critical", "kind": "slo_breach"}) is True
    assert cap["url"] == "https://hooks/x" and "CRITICAL" in cap["text"]
