"""Runtime ACK contract self-check (P-4): server._doctor_report + GET /doctor.

The doctor was CLI-only; now the server exposes the same read-only preflight at runtime
(and logs a summary at boot), so contract drift surfaces live instead of only via tooling.
"""
from __future__ import annotations

import json
import sys
import threading
import types
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=object))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402
import server  # noqa: E402


def test_doctor_report_shape(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rep = server._doctor_report()
    assert set(rep) >= {"ok", "errors", "warnings", "findings"}
    assert isinstance(rep["ok"], bool)
    assert isinstance(rep["errors"], int) and isinstance(rep["warnings"], int)
    assert isinstance(rep["findings"], list)
    # each finding is a serialisable dict with the fail-loud trio keys
    for f in rep["findings"]:
        assert {"check", "severity", "message"} <= set(f)
        assert f["severity"] in {"OK", "WARN", "ERROR", "SKIP"}


def test_doctor_report_clean_on_empty_workspace(tmp_path, monkeypatch):
    # An empty workspace has no duplicate task ids / broken deps → no ERROR findings.
    monkeypatch.chdir(tmp_path)
    rep = server._doctor_report()
    assert rep["errors"] == 0 and rep["ok"] is True


def test_doctor_detects_duplicate_task_id(tmp_path, monkeypatch):
    # Same task id in two status dirs → the reconciler can't disambiguate → ERROR.
    monkeypatch.chdir(tmp_path)
    for status in ("pending", "in_progress"):
        d = tmp_path / "tasks" / status
        d.mkdir(parents=True)
        (d / "KGC-9.json").write_text(
            json.dumps({"id": "KGC-9", "title": "dup", "type": "feature"}),
            encoding="utf-8")
    rep = server._doctor_report()
    assert rep["errors"] >= 1 and rep["ok"] is False
    assert any("KGC-9" in (f.get("message") or "") for f in rep["findings"])


def test_doctor_http_route(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # Default open policy on the handler → /doctor is reachable without a secret.
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server._Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        port = httpd.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/doctor", timeout=5) as r:
            rep = json.loads(r.read().decode())
        assert set(rep) >= {"ok", "errors", "warnings", "findings"}
    finally:
        httpd.shutdown()
