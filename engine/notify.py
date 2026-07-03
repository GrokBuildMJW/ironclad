"""#1083: outbound escalation notification.

Make a HUMAN_ESCALATION actually reach an off-duty human via an operator-configured webhook (a Slack
incoming webhook or any JSON-accepting endpoint). The endpoint is **deploy config**
(``GX10_NOTIFY_WEBHOOK`` / ``notify.webhook``) — NEVER hardcoded in core, so the tree stays secret-free and
boundary-clean; **default-off** (no URL → no consumer registered → byte-identical). stdlib-only, fail-soft
(a notification failure must never break a run). This is the minimal webhook channel; a richer plugin
(Slack API, SMTP email, SMS) can extend the same ``escalation`` hook event.
"""
from __future__ import annotations

import json
import urllib.request
from typing import Any, Callable, Dict, Optional


def notify_webhook(url: str, text: str, *, extra: "Optional[Dict[str, Any]]" = None, timeout: float = 8.0) -> bool:
    """POST ``{"text": text, **extra}`` (Slack-compatible) as JSON to *url*. Returns True on a 2xx response,
    False on an empty url or any error. Never raises."""
    if not (url or "").strip():
        return False
    payload: "Dict[str, Any]" = {"text": text}
    if extra:
        payload.update(extra)
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            return 200 <= int(status) < 300
    except Exception:  # noqa: BLE001 — a notification failure is advisory, never breaks the run
        return False


def escalation_text(ctx: "Optional[Dict[str, Any]]") -> str:
    """The human-facing message for an ``escalation`` event ctx (task_id / attempts / result_cls)."""
    p = ctx or {}
    task = p.get("task_id", "?")
    attempts = p.get("attempts", "?")
    cls = p.get("result_cls")
    tail = f" ({cls})" if cls else ""
    return f"[Ironclad] human escalation on task {task}{tail} after {attempts} attempt(s) — needs a human."


def make_escalation_consumer(url: str) -> "Callable[[Any], None]":
    """A Hook-Bus consumer (signature ``(ctx)``) for the ``escalation`` event that notifies *url*.
    Observer-only + fail-soft; the engine registers it only when a webhook is configured."""
    def _consumer(ctx: "Any" = None) -> None:
        try:
            c = ctx if isinstance(ctx, dict) else {}
            notify_webhook(url, escalation_text(c),
                           extra={"task_id": c.get("task_id"), "attempts": c.get("attempts"),
                                  "kind": "human_escalation"})
        except Exception:  # noqa: BLE001 — never break the escalation path
            pass
    return _consumer
