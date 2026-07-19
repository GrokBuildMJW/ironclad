"""Python client retry boundary for pre-stream /chat/stream failures (#1670)."""
from __future__ import annotations

import io
import json
import sys
import urllib.error
from email.message import Message
from pathlib import Path

import pytest

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import client  # noqa: E402


class _Stream:
    def __init__(self, chunks):
        self._chunks = iter(chunks)
        self.headers = Message()
        self.headers["Content-Type"] = "text/plain"

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read1(self, _size):
        item = next(self._chunks, b"")
        if isinstance(item, BaseException):
            raise item
        return item


def _http_error(code: int, reason: str) -> urllib.error.HTTPError:
    body = io.BytesIO(json.dumps({"ok": False, "error": reason}).encode())
    return urllib.error.HTTPError("http://h/chat/stream", code, reason, Message(), body)


def test_chat_stream_retries_two_pre_stream_503s_then_consumes_stream(monkeypatch):
    outcomes = iter((_http_error(503, "busy one"), _http_error(503, "busy two"),
                     _Stream((b"turn completed", b""))))
    calls = []

    def urlopen(_req, timeout):
        calls.append(timeout)
        outcome = next(outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(client.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(client, "_CHAT_STREAM_RETRY_BASE_S", 0)
    text = []
    retries = []

    client.Server("http://h", timeout=9).chat_stream(
        "hi", text.append, on_retry=lambda *args: retries.append(args))

    assert calls == [9, 9, 9]
    assert text == ["turn completed"]
    assert retries == [("busy one", 0, 2, 3), ("busy two", 0, 3, 3)]


def test_chat_stream_exhausted_503_retries_propagate_last_error(monkeypatch):
    errors = [_http_error(503, f"busy {attempt}") for attempt in range(1, 4)]
    calls = 0

    def urlopen(_req, timeout):
        nonlocal calls
        assert timeout == 9
        error = errors[calls]
        calls += 1
        raise error

    monkeypatch.setattr(client.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(client, "_CHAT_STREAM_RETRY_BASE_S", 0)

    with pytest.raises(urllib.error.HTTPError) as exc:
        client.Server("http://h", timeout=9).chat_stream("hi", lambda _text: None)

    assert exc.value is errors[-1]
    assert calls == 3


def test_chat_stream_4xx_is_not_retried(monkeypatch):
    error = _http_error(400, "missing message")
    calls = 0

    def urlopen(_req, timeout):
        nonlocal calls
        assert timeout == 9
        calls += 1
        raise error

    monkeypatch.setattr(client.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(client, "_CHAT_STREAM_RETRY_BASE_S", 0)

    with pytest.raises(urllib.error.HTTPError) as exc:
        client.Server("http://h", timeout=9).chat_stream("", lambda _text: None)

    assert exc.value is error
    assert calls == 1


def test_chat_stream_mid_stream_failure_is_not_retried(monkeypatch):
    calls = 0

    def urlopen(_req, timeout):
        nonlocal calls
        assert timeout == 9
        calls += 1
        return _Stream((OSError("stream read failed"),))

    monkeypatch.setattr(client.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(client, "_CHAT_STREAM_RETRY_BASE_S", 0)

    with pytest.raises(OSError, match="stream read failed"):
        client.Server("http://h", timeout=9).chat_stream("hi", lambda _text: None)

    assert calls == 1
