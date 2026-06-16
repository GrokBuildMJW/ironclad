"""Server/client split: the headless orchestrator server (core/engine/server.py).

Validates the pieces that make the split work WITHOUT touching the model:
  - the headless capture sink isolates output to the calling thread (a /chat request
    collects exactly its own turn's ``_ui_print`` output; other threads do not leak in)
  - ``_write_feedback`` drops the file the reconciler advances on
  - ``_pending_handovers`` surfaces a staged handover for the client to pull
  - the HTTP routes (/health, /chat, /tasks, /feedback) work end to end against a
    real ThreadingHTTPServer with a stubbed agent + dispatch (no vLLM)
"""
from __future__ import annotations

import json
import sys
import threading
import types
import urllib.request
from pathlib import Path

# Stub the heavy optional dep so the engine imports without openai installed.
sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=object))

# core/engine on sys.path so `import gx10` / `import server` work (conftest adds core/).
_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402
import pytest  # noqa: E402
import server  # noqa: E402


@pytest.fixture(autouse=True)
def _sink():
    """Install the capture sink for the test and restore afterwards."""
    prev = gx10._UI_SINK
    gx10._UI_SINK = server._capture_sink
    yield
    gx10._UI_SINK = prev


# --------------------------------------------------------------------------- #
# Capture sink — per-thread isolation.
# --------------------------------------------------------------------------- #
def test_capture_collects_this_threads_output():
    with server._Captured() as cap:
        gx10._ui_print("alpha")
        gx10._ui_print("beta")
    assert "alpha" in cap.text and "beta" in cap.text


def test_capture_does_not_leak_across_threads():
    other_emitted = threading.Event()

    def _bg():
        # No buffer opened on this thread → must NOT land in the request buffer.
        gx10._ui_print("BACKGROUND_NOISE")
        other_emitted.set()

    with server._Captured() as cap:
        t = threading.Thread(target=_bg)
        t.start()
        assert other_emitted.wait(2.0)
        t.join()
        gx10._ui_print("request_line")
    assert "request_line" in cap.text
    assert "BACKGROUND_NOISE" not in cap.text


# --------------------------------------------------------------------------- #
# Feedback drop + pending discovery (file contract with the reconciler).
# --------------------------------------------------------------------------- #
def test_write_feedback_creates_reconciler_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = server._write_feedback("KGC-7", "opus", "## Result\nok")
    p = Path(path)
    assert p.name == "KGC-7_OPUS-feedback.md"
    assert p.parent.name == "feedback"
    assert "## Result" in p.read_text(encoding="utf-8")


def test_pending_handovers_surfaces_staged_task(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = gx10.TaskStore(root=".")
    store.create({"type": "feature", "priority": "high",
                  "title": "wire it", "description": "do the thing"}, force=True)
    tid = store.list("pending")[0]["id"]
    ho_dir = tmp_path / "summaries" / "handovers"
    ho_dir.mkdir(parents=True)
    (ho_dir / f"{tid}_OPUS.md").write_text("---\nto: claude-opus-4-8\neffort: high\n---\nbody",
                                           encoding="utf-8")
    pend = server._pending_handovers()
    assert len(pend) == 1
    item = pend[0]
    assert item["id"] == tid
    assert item["agent"] == "OPUS"
    assert "body" in item["handover"]
    assert item["model"] == "claude-opus-4-8"
    assert item["effort"] == "high"


# --------------------------------------------------------------------------- #
# HTTP routes end to end (real server, stubbed agent + dispatch).
# --------------------------------------------------------------------------- #
class _StubAgent:
    model = "stub-model"


class _StubWorkers:
    def fanout(self, prompts, *, system=None, max_tokens=None, temperature=0.7, think=True):
        return [{"ok": True, "content": f"r:{p}", "think": think} for p in prompts]


def _start_server(monkeypatch, tmp_path):
    from http.server import ThreadingHTTPServer

    monkeypatch.chdir(tmp_path)

    def _fake_dispatch(agent, message):
        gx10._ui_print(f"echo:{message}")

    monkeypatch.setattr(gx10, "_dispatch", _fake_dispatch)

    server._Handler.agent = _StubAgent()
    server._Handler.cfg = {"connection": {"base_url": "http://localhost:8000/v1"}}
    server._Handler.workers = _StubWorkers()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server._Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


def _get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
        return json.loads(r.read().decode())


def _post(port, path, body):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 data=json.dumps(body).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode())


def test_http_health_and_chat_capture(tmp_path, monkeypatch):
    httpd, port = _start_server(monkeypatch, tmp_path)
    try:
        health = _get(port, "/health")
        assert health["ok"] and health["model"] == "stub-model"

        res = _post(port, "/chat", {"message": "ping"})
        assert res["ok"] and "echo:ping" in res["output"]

        assert _get(port, "/tasks") == {"tasks": []}

        fb = _post(port, "/feedback", {"task_id": "KGC-1", "agent": "OPUS", "content": "done"})
        assert fb["ok"]
        assert Path(fb["feedback_file"]).read_text(encoding="utf-8") == "done"

        fo = _post(port, "/fanout", {"prompts": ["x", "y"], "think": False})
        assert fo["ok"]
        assert [r["content"] for r in fo["results"]] == ["r:x", "r:y"]
        assert fo["results"][0]["think"] is False
    finally:
        httpd.shutdown()


def test_http_fanout_requires_prompts(tmp_path, monkeypatch):
    httpd, port = _start_server(monkeypatch, tmp_path)
    try:
        try:
            _post(port, "/fanout", {"prompts": []})
            assert False, "expected HTTP 400"
        except urllib.error.HTTPError as e:  # type: ignore[name-defined]
            assert e.code == 400
    finally:
        httpd.shutdown()


def test_http_chat_stream(tmp_path, monkeypatch):
    httpd, port = _start_server(monkeypatch, tmp_path)
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/chat/stream",
            data=json.dumps({"message": "ping"}).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=5) as r:
            body = r.read().decode("utf-8")  # liest bis EOF (Connection: close)
        assert "echo:ping" in body
    finally:
        httpd.shutdown()


def test_http_chat_requires_message(tmp_path, monkeypatch):
    httpd, port = _start_server(monkeypatch, tmp_path)
    try:
        try:
            _post(port, "/chat", {"message": "   "})
            assert False, "expected HTTP 400"
        except urllib.error.HTTPError as e:  # type: ignore[name-defined]
            assert e.code == 400
    finally:
        httpd.shutdown()


import urllib.error  # noqa: E402  (used in the 400 assertion above)
