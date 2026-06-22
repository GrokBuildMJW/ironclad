"""GET /catalogue — the loaded prompt/skill registry snapshot for client autocomplete (#149).

Mirrors test_doctor_endpoint / test_security: a real ThreadingHTTPServer with `server._Handler`.
The route reuses `gx10._catalogue_snapshot` (one surface, no re-scan) and is **gated** like
`/tasks`/`/doctor` — the snapshot is deployment detail, not readable without the secret.
"""
from __future__ import annotations

import json
import sys
import threading
import types
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=object))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402
import pytest  # noqa: E402
import server  # noqa: E402
from security import GATED_PATHS, SecurityPolicy, SessionRegistry  # noqa: E402


@pytest.fixture(autouse=True)
def _restore():
    prev_p, prev_s = server._Handler.policy, server._Handler.sessions
    yield
    server._Handler.policy, server._Handler.sessions = prev_p, prev_s
    gx10._PLUGIN_TOOLS.clear()
    gx10._PLAYBOOKS.clear()
    gx10._PROMPTS.clear()


def _serve(policy: SecurityPolicy):
    server._Handler.policy = policy
    server._Handler.sessions = SessionRegistry(policy)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server._Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


def test_catalogue_is_in_gated_paths():
    assert "/catalogue" in GATED_PATHS          # never readable without the secret under token/sealed


def test_catalogue_route_open_returns_loaded_registry():
    gx10._load_skills(None)                      # load the real built-ins
    httpd, port = _serve(SecurityPolicy("open", None, 30, "mount"))
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/catalogue", timeout=5) as r:
            snap = json.loads(r.read().decode())
    finally:
        httpd.shutdown()
    assert set(snap) == {"prompts", "skills"}
    pnames = {p["name"] for p in snap["prompts"]}
    snames = {s["name"] for s in snap["skills"]}
    assert {"code-review", "commit-message", "bug-report", "explain-code"} <= pnames
    assert "mpr_research" in snames
    # shape contract the client relies on
    assert all({"name", "description", "languages"} <= set(p) for p in snap["prompts"])
    assert all({"name", "kind", "description"} <= set(s) for s in snap["skills"])


def test_catalogue_is_gated_under_token(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    httpd, port = _serve(SecurityPolicy("token", "s3cret", 30, "mount"))
    try:
        # no secret → 401
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/catalogue", timeout=5)
        assert ei.value.code == 401
        # query string must not bypass the gate
        with pytest.raises(urllib.error.HTTPError) as eq:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/catalogue?leak=1", timeout=5)
        assert eq.value.code == 401
        # correct secret → 200
        req = urllib.request.Request(f"http://127.0.0.1:{port}/catalogue", method="GET")
        req.add_header("Authorization", "Bearer s3cret")
        with urllib.request.urlopen(req, timeout=5) as r:
            snap = json.loads(r.read().decode())
        assert set(snap) == {"prompts", "skills"}
    finally:
        httpd.shutdown()
