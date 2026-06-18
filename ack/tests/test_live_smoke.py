"""Live smoke suite — exercises a RUNNING orchestrator end to end (real model).

Skipped unless ``GX10_LIVE_URL`` points at a running server (e.g.
``GX10_LIVE_URL=http://host:8100``); set ``GX10_LIVE_TOKEN`` for token/sealed profiles.
Assertions are structural (the model's wording varies) — they prove the *plumbing* is
solid, not a specific answer. Run:  GX10_LIVE_URL=http://host:8100 pytest -k live -q
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request

import pytest

_URL = os.environ.get("GX10_LIVE_URL")
_TOKEN = os.environ.get("GX10_LIVE_TOKEN") or None

pytestmark = pytest.mark.skipif(not _URL, reason="set GX10_LIVE_URL to run the live smoke suite")


def _headers(extra=None):
    h = {"Content-Type": "application/json"}
    if _TOKEN:
        h["Authorization"] = f"Bearer {_TOKEN}"
    h.update(extra or {})
    return h


def _req(method, path, body=None, headers=None, timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(_URL.rstrip("/") + path, data=data, method=method)
    for k, v in _headers(headers).items():
        r.add_header(k, v)
    with urllib.request.urlopen(r, timeout=timeout) as x:
        raw = x.read().decode("utf-8", "replace")
    return json.loads(raw) if raw else {}


@pytest.fixture(scope="module")
def _session():
    """Open a session if the server's profile requires one; yield session headers."""
    h = _req("GET", "/health")
    sec = h.get("security") or {}
    if not sec.get("session"):
        yield {}
        return
    sid = _req("POST", "/session/open", {})["session_id"]
    yield {"X-Session-Id": sid}
    try:
        _req("POST", "/session/close", {"session_id": sid})
    except Exception:
        pass


# ── surface ──────────────────────────────────────────────────
def test_health_shape():
    h = _req("GET", "/health")
    assert h["ok"] is True
    assert h["model"] and h["base_url"]
    assert "security" in h and "sealed" in h


def test_chat_simple(_session):
    res = _req("POST", "/chat", {"message": "Antworte knapp: 2+2?"}, _session)
    assert res["ok"] is True and res["output"].strip()
    assert "4" in res["output"]


def test_chat_tool_turn(_session):
    # A tool-using turn: the agent should call list_directory and answer from it.
    res = _req("POST", "/chat",
               {"message": "Nutze list_directory auf '.' und nenne knapp, was du siehst."},
               _session)
    assert res["ok"] is True
    assert "list_directory" in res["output"]      # the tool was actually invoked


def test_chat_stream(_session):
    body = json.dumps({"message": "Sag in einem Wort: Hallo"}).encode()
    r = urllib.request.Request(_URL.rstrip("/") + "/chat/stream", data=body, method="POST")
    for k, v in _headers(_session).items():
        r.add_header(k, v)
    chunks = []
    with urllib.request.urlopen(r, timeout=120) as resp:
        while True:
            b = resp.read(256)
            if not b:
                break
            chunks.append(b.decode("utf-8", "replace"))
    assert "".join(chunks).strip()                 # streamed until EOF, non-empty


def test_tasks_endpoint(_session):
    out = _req("GET", "/tasks", headers=_session)
    assert isinstance(out.get("tasks"), list)      # snapshot returns a list


def test_fanout_parallel(_session):
    prompts = [f"In einem Wort: Hauptstadt von {c}?"
               for c in ["Frankreich", "Japan", "Italien", "Spanien"]]
    t0 = time.monotonic()
    res = _req("POST", "/fanout", {"prompts": prompts, "max_tokens": 48, "think": False},
               _session)
    wall = time.monotonic() - t0
    rs = res["results"]
    assert len(rs) == len(prompts)
    ok = sum(1 for x in rs if x.get("ok"))
    assert ok == len(prompts)                      # all succeeded
    sumlat = sum(x.get("latency") or 0 for x in rs)
    assert sumlat > wall                            # genuinely concurrent (sum > wall)


def test_fanout_rejects_empty(_session):
    with pytest.raises(urllib.error.HTTPError) as e:
        _req("POST", "/fanout", {"prompts": []}, _session)
    assert e.value.code == 400


def test_cancel_returns_ok(_session):
    res = _req("POST", "/cancel", {}, _session)
    assert res["ok"] is True and res["cancelled"] is True


def test_memory_query_if_up(_session):
    if (_req("GET", "/health").get("memory")) != "up":
        pytest.skip("memory backend not up")
    res = _req("POST", "/chat",
               {"message": "Nutze query_memory mit der Anfrage 'architecture' und "
                           "berichte knapp, ob es Treffer gab."}, _session)
    assert res["ok"] is True and "query_memory" in res["output"]
