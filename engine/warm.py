"""Optional warm tier (Valkey) — short-term session state + a retrieval cache.

The volatile tier BETWEEN the bounded model window (Hot) and the long-term Mem0 store (Cold):

  * **Session state** — ``session:{id}:summary`` / ``:tokens`` / ``:recent``, TTL'd. Unlike the
    in-process window it **survives an orchestrator restart** and is **shared across the parallel
    reasoning workers** (which otherwise share no history). This is the durable backing for the
    rolling summary (B1).
  * **Retrieval cache (cache-aside)** — ``ret:{sha1(query)}`` → a ``/search`` result, short TTL, so a
    repeat/follow-up turn skips the slow vector(+graph) round-trip (B2).

The binding design rule: **additive-only, fail-soft, off-critical-path.** Every operation degrades
to a no-op on any error or when Valkey / the client is unavailable — it can never block or break a
turn (a hard ``socket_timeout`` keeps a stalled tier from holding up a read). Writes are meant to be
fire-and-forget; reads are cache-or-fall-through.

Secret-free: the endpoint comes from config / ``GX10_WARM_URL`` at runtime, never hard-coded. Uses
the MIT ``redis`` client (Valkey speaks the Redis wire protocol), **lazily imported** so the base
engine has no hard dependency. License note: use **Valkey** (BSD-3, Linux-Foundation fork of Redis
7.2) or KeyDB (BSD-3) for the *server* — NOT the Redis server (RSALv2/SSPL since 2024); the *client*
``redis`` / redis-py is MIT.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional


class WarmTier:
    """Optional Valkey-backed warm tier. All ops are fail-soft: unavailable / error → no-op."""

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        config = config or {}
        self.url = str(config.get("url") or "").strip()
        self.enabled = bool(config.get("enabled", bool(self.url)))
        self.session_ttl = int(config.get("session_ttl", 86_400))  # 24h — session state
        self.cache_ttl = int(config.get("cache_ttl", 180))         # 3 min — retrieval cache
        # hard time budget: a stalled warm tier must never hold up a turn (cache-or-fall-through).
        self.timeout = float(config.get("timeout", 0.5))
        self._client: Any = None
        self._tried = False

    # ── connection (lazy, fail-soft) ─────────────────────────────
    def _conn(self) -> Any:
        """Connect once. Returns the client or None. A missing ``redis`` dep or an unreachable
        server disables the tier (no-op) rather than raising."""
        if not self.enabled or not self.url:
            return None
        if self._client is not None or self._tried:
            return self._client
        self._tried = True
        try:
            import redis  # MIT client; optional — only imported when the warm tier is configured
            self._client = redis.Redis.from_url(
                self.url,
                socket_timeout=self.timeout,
                socket_connect_timeout=self.timeout,
                decode_responses=True,
            )
            self._client.ping()  # surface an unreachable server now → fall back to no-op
        except Exception:  # noqa: BLE001 — missing dep / unreachable → disabled
            self._client = None
        return self._client

    def is_available(self) -> bool:
        c = self._conn()
        if c is None:
            return False
        try:
            return bool(c.ping())
        except Exception:  # noqa: BLE001
            self._client = None  # drop a dead connection so a later call can retry the URL
            return False

    # ── session state (survives restart + shared across workers) ──
    def get_session(self, sid: str, field: str) -> Optional[str]:
        c = self._conn()
        if c is None or not sid:
            return None
        try:
            return c.get(f"session:{sid}:{field}")
        except Exception:  # noqa: BLE001
            return None

    def set_session(self, sid: str, field: str, value: str, ttl: Optional[int] = None) -> bool:
        c = self._conn()
        if c is None or not sid:
            return False
        try:
            c.set(f"session:{sid}:{field}", value, ex=int(ttl or self.session_ttl))
            return True
        except Exception:  # noqa: BLE001
            return False

    def del_session(self, sid: str, field: str) -> bool:
        """Drop a session field (MEM-12: e.g. the rolling summary on /reset). Fail-soft → False."""
        c = self._conn()
        if c is None or not sid:
            return False
        try:
            c.delete(f"session:{sid}:{field}")
            return True
        except Exception:  # noqa: BLE001
            return False

    # ── retrieval cache (cache-aside in front of the cold vector store) ──
    @staticmethod
    def _key(query: str) -> str:
        return "ret:" + hashlib.sha1(query.encode("utf-8")).hexdigest()

    def cache_get(self, query: str) -> Optional[List[str]]:
        """Cached ``/search`` results for *query*, or None on miss / unavailable."""
        c = self._conn()
        if c is None or not query.strip():
            return None
        try:
            raw = c.get(self._key(query))
            return list(json.loads(raw)) if raw else None
        except Exception:  # noqa: BLE001
            return None

    def cache_set(self, query: str, results: List[str], ttl: Optional[int] = None) -> bool:
        c = self._conn()
        if c is None or not query.strip():
            return False
        try:
            c.set(self._key(query), json.dumps(list(results)), ex=int(ttl or self.cache_ttl))
            return True
        except Exception:  # noqa: BLE001
            return False
