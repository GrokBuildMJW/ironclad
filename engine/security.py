"""Phase-d security: selectable trust profiles + a session lifecycle (single-tenant).

> **One operator, one principal.** Ironclad is single-tenant today (see
> ``docs/roadmap.md``). Nothing here authenticates a *user* — the optional token is a
> **deployment secret** ("is this my client process?"), not a login. Multi-user
> identity/authorization is a separate, unbuilt phase (Phase g). Keep that honest.

This module is the mechanism; the *policy* is chosen by config (``security.profile``):

  * ``open``   — today's behaviour: no auth, bind as requested, code-mount allowed.
                 Out-of-the-box, maximum transparency (the OSS default).
  * ``token``  — a shared **deployment secret** (``Authorization: Bearer``) over the LAN.
  * ``sealed`` — bind ``127.0.0.1`` only (meant to sit behind a client-managed tunnel),
                 secret **required**, plus an explicit **session**: the client opens one,
                 heartbeats it, and closes it on exit. With no live session the server is
                 **sealed** — client-facing endpoints refuse and background planning
                 pauses. Code-locality is enforced (pull-only, no mount).

Secret-free and stdlib-only: the token *value* comes from the environment at runtime
(named by ``security.token_env``), never hard-coded; transport specifics (SSH, hosts)
live in the operator's private config, never here.
"""
from __future__ import annotations

import hmac
import os
import secrets
import threading
import time
from typing import Any, Dict, Optional

#: Profiles, weakest → strongest. ``open`` is the OSS default.
PROFILES = ("open", "token", "sealed")

#: Endpoints that require authorization (and, where applicable, a live session).
#: ``/health`` and the ``/session/*`` lifecycle routes are intentionally excluded
#: (``/session/open`` checks the token itself; ``/health`` is the liveness + handshake
#: probe the client needs *before* it has a session — it leaks only the profile shape,
#: never the token). ``/tasks`` IS gated: the TaskStore snapshot carries task titles and
#: descriptions, which must not be readable without the deployment secret.
GATED_PATHS = ("/chat", "/chat/stream", "/tool-result", "/fanout", "/cancel",
               "/tasks", "/pending", "/feedback", "/doctor")


class SecurityPolicy:
    """Resolved trust policy for one server process. Derived once at bootstrap from the
    merged config + environment; immutable thereafter."""

    def __init__(self, profile: str, token: Optional[str], heartbeat_s: int,
                 code_locality: str) -> None:
        self.profile = profile if profile in PROFILES else "open"
        self.token = token or None
        self.heartbeat_s = max(5, int(heartbeat_s))
        # open allows a code mount; sealed forces pull-only/local. token leaves it as set.
        if self.profile == "sealed":
            code_locality = "local"
        self.code_locality = code_locality if code_locality in ("local", "mount") else "mount"

    # ── derivation ───────────────────────────────────────────
    @classmethod
    def from_config(cls, cfg: Dict[str, Any]) -> "SecurityPolicy":
        sec = dict((cfg or {}).get("security") or {})
        profile = (os.environ.get("GX10_PROFILE") or sec.get("profile") or "open").strip().lower()
        token_env = sec.get("token_env") or "GX10_SERVER_TOKEN"
        token = os.environ.get(token_env) or None
        hb = os.environ.get("GX10_SESSION_HEARTBEAT") or sec.get("session_heartbeat_s") or 30
        try:
            hb = int(hb)
        except (TypeError, ValueError):
            hb = 30
        code_locality = (sec.get("code_locality") or "mount").strip().lower()
        return cls(profile, token, hb, code_locality)

    # ── profile-derived flags ────────────────────────────────
    @property
    def auth_required(self) -> bool:
        return self.profile in ("token", "sealed")

    @property
    def session_required(self) -> bool:
        return self.profile == "sealed"

    @property
    def seals_when_idle(self) -> bool:
        return self.profile == "sealed"

    def effective_bind(self, requested: str) -> str:
        """``sealed`` is reachable only via loopback (a tunnel terminates here); other
        profiles honour the requested bind (default LAN-wide, like the model port)."""
        return "127.0.0.1" if self.profile == "sealed" else requested

    def startup_error(self) -> Optional[str]:
        """Fail-closed: a profile that demands a secret must have one, or refuse to boot."""
        if self.auth_required and not self.token:
            env = "GX10_SERVER_TOKEN"
            return (f"security.profile={self.profile!r} requires a deployment secret but "
                    f"none is set — export {env}=… (a shared secret, not a user login).")
        return None

    # ── request-time check ───────────────────────────────────
    def check_token(self, auth_header: Optional[str]) -> bool:
        """Constant-time bearer comparison. Always True when no auth is required."""
        if not self.auth_required:
            return True
        if not self.token or not auth_header:
            return False
        prefix = "Bearer "
        presented = auth_header[len(prefix):] if auth_header.startswith(prefix) else auth_header
        return hmac.compare_digest(presented.strip(), self.token)

    def summary(self) -> Dict[str, Any]:
        """Non-secret shape advertised on ``/health`` so the client knows how to connect.
        Never leaks the token itself — only whether one is needed."""
        return {
            "profile": self.profile,
            "auth": self.auth_required,
            "session": self.session_required,
            "heartbeat_s": self.heartbeat_s,
            "code_locality": self.code_locality,
        }


class SessionRegistry:
    """Tracks live operator sessions. A session is *live* while it has been seen within
    ``2 × heartbeat`` seconds; the server is *sealed* when the profile demands a session
    and none is live. Thread-safe — the HTTP server is threaded."""

    def __init__(self, policy: SecurityPolicy, now=time.monotonic) -> None:
        self.policy = policy
        self._now = now
        self._lock = threading.Lock()
        self._sessions: Dict[str, float] = {}  # session_id → last-seen monotonic ts

    @property
    def _ttl(self) -> float:
        return 2.0 * self.policy.heartbeat_s

    def open(self) -> Dict[str, Any]:
        sid = secrets.token_urlsafe(18)
        with self._lock:
            self._sessions[sid] = self._now()
        return {"session_id": sid, "heartbeat_s": self.policy.heartbeat_s}

    def heartbeat(self, session_id: str) -> bool:
        with self._lock:
            if session_id in self._sessions:
                self._sessions[session_id] = self._now()
                return True
        return False

    def close(self, session_id: str) -> bool:
        with self._lock:
            return self._sessions.pop(session_id, None) is not None

    def is_live(self, session_id: Optional[str]) -> bool:
        if not session_id:
            return False
        with self._lock:
            seen = self._sessions.get(session_id)
            if seen is None:
                return False
            if self._now() - seen > self._ttl:
                self._sessions.pop(session_id, None)
                return False
            return True

    def any_live(self) -> bool:
        cutoff = self._now() - self._ttl
        with self._lock:
            # Opportunistically drop expired sessions.
            self._sessions = {s: t for s, t in self._sessions.items() if t > cutoff}
            return bool(self._sessions)

    def is_sealed(self) -> bool:
        """Sealed = the profile requires a session and none is currently live. In
        non-sealing profiles the server is never sealed."""
        if not self.policy.seals_when_idle:
            return False
        return not self.any_live()

    # ── the one gate every protected route calls ─────────────
    def authorize(self, path: str, auth_header: Optional[str],
                  session_id: Optional[str]) -> Optional[Dict[str, Any]]:
        """Return ``None`` if the request may proceed, else a ``{code, error}`` refusal.
        Order: token first (cheap, no info leak), then session/seal."""
        if path not in GATED_PATHS:
            return None
        if not self.policy.check_token(auth_header):
            return {"code": 401, "error": "missing or invalid deployment secret"}
        if self.policy.session_required and not self.is_live(session_id):
            return {"code": 401, "error": "no live session — channel sealed (open a session first)"}
        return None
