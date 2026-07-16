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
        except Exception:  # noqa: BLE001 — missing dep → permanently disabled; keep the latch (no repeated import)
            self._client = None
            return None
        try:
            self._client = redis.Redis.from_url(
                self.url,
                socket_timeout=self.timeout,
                socket_connect_timeout=self.timeout,
                decode_responses=True,
            )
            self._client.ping()  # surface an unreachable server now → fall back to no-op
        except Exception:  # noqa: BLE001 — unreachable/transient failure → clear the latch so a later _conn()
            # re-dials the URL instead of no-op'ing forever after a first-connect blip (#1556; the ping-failure
            # path in is_available already does this, but a failed INITIAL connect never reached it).
            self._client = None
            self._tried = False
        return self._client

    def is_available(self) -> bool:
        c = self._conn()
        if c is None:
            return False
        try:
            return bool(c.ping())
        except Exception:  # noqa: BLE001
            # WARM-1 (#503): drop the dead connection AND clear the _tried latch so a later _conn()
            # actually re-attempts the URL — otherwise _conn short-circuits on _tried and returns None
            # forever after a transient Valkey blip (the documented retry was impossible).
            self._client = None
            self._tried = False
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

    # ── scope-targeted forget (drop a whole partition's warm state; ADR-0011 D5 / #601 S14-5) ──
    def forget_scope(self, scope: str) -> int:
        """Delete ALL warm state for partition *scope* — every ``session:{scope}:{field}`` AND every
        ``ret:{scope}:{sha1}`` retrieval-cache entry (the warm half of the scope-aware forget). **EXACT
        scope** (matches the cold ``delete_all agent_id==scope``): a deeper track scope (``…::track::x``)
        lands under the same key prefix but its remainder carries another ``:`` and is skipped, so forgetting
        a project never cascades into its tracks. Fail-soft (unavailable / error → the count so far).
        **Fail-closed on an empty / glob-bearing scope** (returns 0 without scanning) so a forget can never
        sweep the whole keyspace or the legacy base keys (``ret:<sha1>`` / ``session:<global-id>:*``).
        Returns the number of keys deleted."""
        c = self._conn()
        scope = (scope or "").strip()
        if c is None or not scope or any(ch in scope for ch in "*?[]\\"):
            return 0
        deleted = 0
        try:
            for prefix in (f"session:{scope}:", f"ret:{scope}:"):
                batch: List[str] = []
                for key in c.scan_iter(match=f"{prefix}*", count=500):
                    if ":" in key[len(prefix):]:   # a deeper track scope under the same prefix — not this scope
                        continue
                    batch.append(key)
                    if len(batch) >= 500:
                        deleted += int(c.delete(*batch) or 0)
                        batch = []
                if batch:
                    deleted += int(c.delete(*batch) or 0)
        except Exception:  # noqa: BLE001 — fail-soft: a forget must never break a turn
            return deleted
        return deleted

    # ── retrieval cache (cache-aside in front of the cold vector store) ──
    @staticmethod
    def _key(query: str, namespace: str = "") -> str:
        # namespace scopes the cache to the active memory partition (mem_ns) so a cached hit for one
        # project never returns for another (ADR-0011 AD-1 / S3b). An EMPTY namespace keeps the LEGACY
        # ``ret:<sha1>`` key byte-identical (no-ctx behaviour unchanged); a set namespace -> ``ret:<ns>:<sha1>``.
        h = hashlib.sha1(query.encode("utf-8")).hexdigest()
        return f"ret:{namespace}:{h}" if namespace else f"ret:{h}"

    def cache_get(self, query: str, namespace: str = "") -> Optional[List[str]]:
        """Cached ``/search`` results for *query* in *namespace*, or None on miss / unavailable."""
        c = self._conn()
        if c is None or not query.strip():
            return None
        try:
            raw = c.get(self._key(query, namespace))
            return list(json.loads(raw)) if raw else None
        except Exception:  # noqa: BLE001
            return None

    def cache_set(self, query: str, results: List[str], namespace: str = "", ttl: Optional[int] = None) -> bool:
        c = self._conn()
        if c is None or not query.strip():
            return False
        try:
            c.set(self._key(query, namespace), json.dumps(list(results)), ex=int(ttl or self.cache_ttl))
            return True
        except Exception:  # noqa: BLE001
            return False
