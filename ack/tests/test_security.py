"""Phase-d security: trust profiles, the session registry, and the HTTP gate.

Single-tenant by design — the token is a *deployment secret*, not a user login (see
docs/roadmap.md). These tests pin:
  - profile derivation + flags (open / token / sealed) and the fail-closed startup
  - constant-time token check and the sealed loopback bind
  - the session lifecycle (open → live → expiry/close → sealed) on a controllable clock
  - the HTTP gate end to end against a real ThreadingHTTPServer (no vLLM):
      token  → 401 without the secret, 200 with it
      sealed → 401 until a session is open, 200 while live, 401 again after close
      /health advertises the profile + sealed state; /session/open needs the secret
"""
from __future__ import annotations

import json
import sys
import threading
import types
import urllib.error
import urllib.request
from pathlib import Path

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=object))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402
import pytest  # noqa: E402
import server  # noqa: E402
from security import SecurityPolicy, SessionRegistry  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_handler_policy():
    """``_Handler.policy``/``sessions`` are class attributes — restore the open defaults
    after each test so a gated profile here never leaks into other test modules."""
    prev_p, prev_s = server._Handler.policy, server._Handler.sessions
    yield
    server._Handler.policy, server._Handler.sessions = prev_p, prev_s


# --------------------------------------------------------------------------- #
# SecurityPolicy — derivation, flags, fail-closed.
# --------------------------------------------------------------------------- #
def test_open_profile_is_permissive():
    p = SecurityPolicy.from_config({"security": {"profile": "open"}})
    assert p.profile == "open"
    assert p.auth_required is False
    assert p.session_required is False
    assert p.seals_when_idle is False
    assert p.check_token(None) is True              # nothing required
    assert p.effective_bind("0.0.0.0") == "0.0.0.0"  # honours requested bind
    assert p.code_locality == "mount"
    assert p.startup_error() is None


def test_token_profile_requires_secret(monkeypatch):
    monkeypatch.delenv("GX10_SERVER_TOKEN", raising=False)
    monkeypatch.delenv("GX10_PROFILE", raising=False)
    p = SecurityPolicy.from_config({"security": {"profile": "token"}})
    assert p.auth_required is True and p.session_required is False
    assert p.startup_error() is not None            # fail-closed: no secret set
    monkeypatch.setenv("GX10_SERVER_TOKEN", "s3cret")
    p2 = SecurityPolicy.from_config({"security": {"profile": "token"}})
    assert p2.startup_error() is None
    assert p2.check_token("Bearer s3cret") is True
    assert p2.check_token("Bearer wrong") is False
    assert p2.check_token("s3cret") is True          # bare token accepted too
    assert p2.check_token(None) is False


def test_invalid_profile_refuses_boot_not_silent_open(monkeypatch):
    # SEC-1 (#503): an unknown NON-EMPTY profile (a typo) must REFUSE to boot, not silently downgrade to the
    # weakest 'open' (fail-open in a fail-closed module). Unset/empty still defaults to 'open'.
    monkeypatch.delenv("GX10_SERVER_TOKEN", raising=False)
    monkeypatch.delenv("GX10_PROFILE", raising=False)
    bad = SecurityPolicy.from_config({"security": {"profile": "seald"}})    # typo of 'sealed'
    assert bad.profile != "open"                        # NOT silently downgraded to the weakest profile
    err = bad.startup_error()
    assert err is not None and "seald" in err           # refuses to boot, names the bad value
    empty = SecurityPolicy.from_config({"security": {"profile": ""}})
    assert empty.profile == "open" and empty.startup_error() is None   # unset → documented default
    assert SecurityPolicy.from_config({}).profile == "open"            # missing section → default


def test_sealed_profile_forces_loopback_and_local(monkeypatch):
    monkeypatch.setenv("GX10_SERVER_TOKEN", "tok")
    p = SecurityPolicy.from_config({"security": {"profile": "sealed", "code_locality": "mount"}})
    assert p.auth_required and p.session_required and p.seals_when_idle
    assert p.effective_bind("0.0.0.0") == "127.0.0.1"   # tunnel terminates on loopback
    assert p.code_locality == "local"                    # sealed forces pull-only
    assert p.startup_error() is None


def test_env_overrides_config_profile(monkeypatch):
    monkeypatch.setenv("GX10_PROFILE", "token")
    monkeypatch.setenv("GX10_SERVER_TOKEN", "tok")
    p = SecurityPolicy.from_config({"security": {"profile": "open"}})
    assert p.profile == "token"


def test_summary_never_leaks_token(monkeypatch):
    monkeypatch.setenv("GX10_SERVER_TOKEN", "super-secret")
    p = SecurityPolicy.from_config({"security": {"profile": "token"}})
    s = p.summary()
    assert s == {"profile": "token", "auth": True, "session": False,
                 "heartbeat_s": 30, "code_locality": "mount"}
    assert "super-secret" not in json.dumps(s)


# --------------------------------------------------------------------------- #
# SessionRegistry — lifecycle on a controllable clock.
# --------------------------------------------------------------------------- #
class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0
    def __call__(self) -> float:
        return self.t


def _sealed_registry():
    clk = _Clock()
    pol = SecurityPolicy("sealed", "tok", heartbeat_s=10, code_locality="local")
    return SessionRegistry(pol, now=clk), clk


def test_session_open_makes_channel_unsealed():
    reg, clk = _sealed_registry()
    assert reg.is_sealed() is True                  # no session yet
    sid = reg.open()["session_id"]
    assert reg.is_live(sid) is True
    assert reg.is_sealed() is False


def test_session_expires_after_two_heartbeats():
    reg, clk = _sealed_registry()                   # heartbeat 10s → ttl 20s
    sid = reg.open()["session_id"]
    clk.t += 15                                      # within ttl
    assert reg.is_live(sid) is True
    clk.t += 10                                      # now 25s > 20s ttl
    assert reg.is_live(sid) is False
    assert reg.is_sealed() is True


def test_heartbeat_keeps_session_live():
    reg, clk = _sealed_registry()
    sid = reg.open()["session_id"]
    clk.t += 15
    assert reg.heartbeat(sid) is True               # refresh
    clk.t += 15                                      # 15s since refresh < 20s ttl
    assert reg.is_live(sid) is True


def test_close_seals_immediately():
    reg, clk = _sealed_registry()
    sid = reg.open()["session_id"]
    assert reg.close(sid) is True
    assert reg.is_live(sid) is False
    assert reg.is_sealed() is True
    assert reg.close(sid) is False                   # idempotent


def test_open_profile_never_seals():
    pol = SecurityPolicy("open", None, 30, "mount")
    reg = SessionRegistry(pol)
    assert reg.is_sealed() is False                  # open never seals
    assert reg.authorize("/chat", None, None) is None


def test_authorize_token_then_session():
    reg, clk = _sealed_registry()
    # No token → refused on the token check first.
    r = reg.authorize("/chat", None, None)
    assert r and r["code"] == 401 and "secret" in r["error"]
    # Right token but no live session → sealed refusal.
    r = reg.authorize("/chat", "Bearer tok", "nope")
    assert r and r["code"] == 401 and "sealed" in r["error"]
    # Token + live session → allowed.
    sid = reg.open()["session_id"]
    assert reg.authorize("/chat", "Bearer tok", sid) is None
    # /tasks is gated (leaks task titles/descriptions) — refused without the secret.
    assert reg.authorize("/tasks", None, None) is not None
    # /health is the open handshake probe — always allowed.
    assert reg.authorize("/health", None, None) is None


# --------------------------------------------------------------------------- #
# HTTP gate end to end (real server, stubbed agent + dispatch).
# --------------------------------------------------------------------------- #
class _StubAgent:
    model = "stub-model"


class _StubWorkers:
    def fanout(self, prompts, *, system=None, max_tokens=None, temperature=0.7, think=True):
        return [{"ok": True, "content": f"r:{p}"} for p in prompts]


def _start(monkeypatch, tmp_path, policy):
    from http.server import ThreadingHTTPServer
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(gx10, "_dispatch", lambda agent, message: gx10._ui_print(f"echo:{message}"))
    monkeypatch.setattr(gx10, "_UI_SINK", server._capture_sink, raising=False)
    server._Handler.agent = _StubAgent()
    server._Handler.cfg = {"connection": {"base_url": "http://localhost:8000/v1"}}
    server._Handler.workers = _StubWorkers()
    server._Handler.policy = policy
    server._Handler.sessions = SessionRegistry(policy)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server._Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


def _post(port, path, body, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 data=json.dumps(body).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode())


def _get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
        return json.loads(r.read().decode())


def test_token_profile_gates_chat(tmp_path, monkeypatch):
    pol = SecurityPolicy("token", "tok", 30, "mount")
    httpd, port = _start(monkeypatch, tmp_path, pol)
    try:
        # /health is open and advertises the profile (no secret needed).
        h = _get(port, "/health")
        assert h["security"]["profile"] == "token" and h["sealed"] is False
        # No bearer → 401.
        with pytest.raises(urllib.error.HTTPError) as ei:
            _post(port, "/chat", {"message": "ping"})
        assert ei.value.code == 401
        # Correct bearer → 200.
        res = _post(port, "/chat", {"message": "ping"},
                    headers={"Authorization": "Bearer tok"})
        assert res["ok"] and "echo:ping" in res["output"]
        # /tasks is gated too — 401 without, 200 with the secret.
        with pytest.raises(urllib.error.HTTPError) as et:
            _get(port, "/tasks")
        assert et.value.code == 401
        # Path normalization: a query string must NOT bypass the gate (regression).
        with pytest.raises(urllib.error.HTTPError) as eq:
            _get(port, "/tasks?leak=1")
        assert eq.value.code == 401
        req = urllib.request.Request(f"http://127.0.0.1:{port}/tasks", method="GET")
        req.add_header("Authorization", "Bearer tok")
        with urllib.request.urlopen(req, timeout=5) as r:
            assert json.loads(r.read().decode()) == {"tasks": []}
    finally:
        httpd.shutdown()


def test_sealed_profile_session_lifecycle(tmp_path, monkeypatch):
    pol = SecurityPolicy("sealed", "tok", 30, "local")
    httpd, port = _start(monkeypatch, tmp_path, pol)
    try:
        assert _get(port, "/health")["sealed"] is True        # no session yet
        # Gated even with the right secret while sealed.
        with pytest.raises(urllib.error.HTTPError) as ei:
            _post(port, "/chat", {"message": "ping"}, headers={"Authorization": "Bearer tok"})
        assert ei.value.code == 401
        # Opening a session needs the secret.
        with pytest.raises(urllib.error.HTTPError) as ei2:
            _post(port, "/session/open", {})
        assert ei2.value.code == 401
        opened = _post(port, "/session/open", {}, headers={"Authorization": "Bearer tok"})
        sid = opened["session_id"]
        assert _get(port, "/health")["sealed"] is False
        # Now /chat works with secret + session header.
        res = _post(port, "/chat", {"message": "ping"},
                    headers={"Authorization": "Bearer tok", "X-Session-Id": sid})
        assert res["ok"] and "echo:ping" in res["output"]
        # Close → sealed again.
        _post(port, "/session/close", {"session_id": sid}, headers={"Authorization": "Bearer tok"})
        assert _get(port, "/health")["sealed"] is True
        with pytest.raises(urllib.error.HTTPError) as ei3:
            _post(port, "/chat", {"message": "ping"},
                  headers={"Authorization": "Bearer tok", "X-Session-Id": sid})
        assert ei3.value.code == 401
    finally:
        httpd.shutdown()


def test_client_session_machinery_against_real_server(tmp_path, monkeypatch):
    """The client's own Server class (headers + session lifecycle) against a real sealed
    server — closes the client-side runtime gap without a model or SSH."""
    import client  # noqa: E402
    pol = SecurityPolicy("sealed", "tok", 30, "local")
    httpd, port = _start(monkeypatch, tmp_path, pol)
    try:
        srv = client.Server(f"http://127.0.0.1:{port}", token="tok")
        assert srv.health()["security"]["session"] is True
        # Gated before a session exists (right secret, no session → sealed).
        with pytest.raises(urllib.error.HTTPError) as e:
            srv.tasks()
        assert e.value.code == 401
        # Open a session → gated route now works; heartbeat keeps it; close re-seals.
        srv.session_open()
        assert srv.session_id
        assert srv.tasks() == []
        assert srv.session_heartbeat() is True
        srv.session_close()
        assert srv.session_id is None
        with pytest.raises(urllib.error.HTTPError) as e2:
            srv.tasks()
        assert e2.value.code == 401
    finally:
        httpd.shutdown()
