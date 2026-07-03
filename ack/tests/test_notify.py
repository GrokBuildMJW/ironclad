"""#1083 (epic #1043 quick-win): outbound escalation notification.

A HUMAN_ESCALATION now fires the `escalation` Hook-Bus event; when a webhook is configured (deploy secret
via GX10_NOTIFY_WEBHOOK / notify.webhook — never a URL literal in core) the notifier POSTs it to an off-duty
human. Default-off (no URL → no consumer registered → byte-identical). Fail-soft.
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=object))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import notify  # noqa: E402
from ack import hooks  # noqa: E402


def test_escalation_is_a_canonical_hook_event():
    assert "escalation" in hooks.HOOK_EVENTS


class _Resp:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_notify_webhook_posts_slack_compatible_json(monkeypatch):
    cap = {}

    def fake_urlopen(req, timeout=None):
        cap["url"] = req.full_url
        cap["data"] = req.data
        cap["method"] = req.get_method()
        return _Resp()

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    assert notify.notify_webhook("https://hooks.example/x", "hello", extra={"kind": "test"}) is True
    body = json.loads(cap["data"])
    assert body["text"] == "hello" and body["kind"] == "test"
    assert cap["method"] == "POST" and cap["url"] == "https://hooks.example/x"


def test_notify_webhook_empty_url_is_noop():
    assert notify.notify_webhook("", "x") is False


def test_notify_webhook_swallows_errors(monkeypatch):
    def boom(*a, **k):
        raise OSError("endpoint down")

    monkeypatch.setattr(notify.urllib.request, "urlopen", boom)
    assert notify.notify_webhook("https://x/y", "z") is False       # never raises → run continues


def test_escalation_text_names_the_task_and_attempts():
    t = notify.escalation_text({"task_id": "KGC-9", "attempts": 3, "result_cls": "timeout"})
    assert "KGC-9" in t and "3 attempt" in t and "timeout" in t


def test_consumer_forwards_to_the_webhook(monkeypatch):
    cap = {}
    monkeypatch.setattr(notify, "notify_webhook",
                        lambda url, text, **k: cap.update(url=url, text=text) or True)
    notify.make_escalation_consumer("https://hooks/x")({"task_id": "T1", "attempts": 2})
    assert cap["url"] == "https://hooks/x" and "T1" in cap["text"]


def test_apply_notify_registers_when_configured_and_clears_when_empty():
    import gx10
    before = len(hooks._HOOKS.get("escalation", ()))
    gx10._apply_notify({"notify": {"webhook": "https://hooks/x"}})
    assert gx10._NOTIFY_CONSUMER is not None
    assert len(hooks._HOOKS.get("escalation", ())) == before + 1
    gx10._apply_notify({"notify": {"webhook": ""}})                 # empty → unregister (default-off)
    assert gx10._NOTIFY_CONSUMER is None
    assert len(hooks._HOOKS.get("escalation", ())) == before
