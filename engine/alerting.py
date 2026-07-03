"""#1061: alerting pipeline.

Turn the runtime telemetry SLO/anomaly verdict (#1060) and inbound external alerts into paged notifications
(delivered via the #1083 webhook). This module is the PURE core — deriving alerts from a `/metrics` report,
normalizing an inbound alert, and formatting a webhook line; the transport (`notify_webhook`), the inbound
receiver (`POST /alert`), the periodic self-scan and the deploy correlation are wired in the engine/server.
stdlib-only; deterministic.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

_SEVERITIES = ("info", "warning", "critical")


def evaluate(report: "Optional[Dict[str, Any]]", *, version: str = "") -> "List[Dict[str, Any]]":
    """Derive alerts from a `/metrics` report (#1060): each SLO breach → a `critical` alert, each anomaly
    signal → a `warning` alert. Every alert is correlated with the running deploy *version*. No traffic /
    all-green → []. Pure."""
    report = report or {}
    tag = f" [deploy {version}]" if version else ""
    alerts: "List[Dict[str, Any]]" = []
    for b in ((report.get("slo") or {}).get("breaches") or []):
        alerts.append({"severity": "critical", "kind": "slo_breach", "message": f"SLO breach: {b}{tag}",
                       "version": version})
    for s in ((report.get("anomaly") or {}).get("signals") or []):
        alerts.append({"severity": "warning", "kind": "anomaly", "message": f"Anomaly: {s}{tag}",
                       "version": version})
    return alerts


def format_alert(alert: "Dict[str, Any]") -> str:
    """A Slack/webhook-ready one-liner for an alert."""
    sev = str(alert.get("severity", "info")).upper()
    return f"[Ironclad][{sev}] {alert.get('message', '')}"


def normalize_inbound(payload: "Any") -> "Tuple[Optional[Dict[str, Any]], Optional[str]]":
    """Validate + normalize an EXTERNAL alert (POSTed to `/alert`) into the internal alert shape. Returns
    ``(alert, None)`` or ``(None, reason)``. Clamps severity to the known set; requires a non-empty message."""
    if not isinstance(payload, dict):
        return None, "alert must be a JSON object"
    msg = str(payload.get("message", "")).strip()
    if not msg:
        return None, "alert needs a non-empty 'message'"
    sev = str(payload.get("severity", "warning")).lower()
    if sev not in _SEVERITIES:
        sev = "warning"
    return {"severity": sev, "kind": str(payload.get("kind", "external")), "message": msg,
            "source": str(payload.get("source", "external"))}, None
