"""Optional warm tier (engine/warm.py) — fail-soft session state + retrieval cache.

Validates the contract with an INJECTED fake client (no live Valkey, no ``redis`` dep needed):
disabled-when-unconfigured, session/cache round-trips, the query→key hashing, and — the load-bearing
property — that EVERY op degrades to a no-op (never raises) when the backing client is dead.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

from warm import WarmTier  # noqa: E402


class FakeValkey:
    """Minimal in-memory stand-in for the redis client (get/set/ping)."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.last_ex: int | None = None

    def ping(self) -> bool:
        return True

    def get(self, k: str):
        return self.store.get(k)

    def set(self, k: str, v: str, ex=None) -> None:
        self.store[k] = v
        self.last_ex = ex

    def delete(self, k: str) -> int:
        return 1 if self.store.pop(k, None) is not None else 0


class DeadValkey:
    """A client whose every call raises — the warm tier must swallow it."""

    def ping(self):
        raise OSError("down")

    def get(self, k):
        raise OSError("down")

    def set(self, k, v, ex=None):
        raise OSError("down")

    def delete(self, k):
        raise OSError("down")


def _wired(client) -> WarmTier:
    wt = WarmTier({"url": "redis://x", "enabled": True})
    wt._client = client  # bypass the lazy connect with an injected client
    wt._tried = True
    return wt


def test_disabled_when_unconfigured() -> None:
    wt = WarmTier({})
    assert wt.is_available() is False
    assert wt.get_session("s", "summary") is None
    assert wt.set_session("s", "summary", "x") is False
    assert wt.cache_get("q") is None
    assert wt.cache_set("q", ["a"]) is False


def test_session_roundtrip_with_ttl() -> None:
    fake = FakeValkey()
    wt = _wired(fake)
    assert wt.is_available() is True
    assert wt.set_session("sid", "summary", "hello", ttl=99) is True
    assert wt.get_session("sid", "summary") == "hello"
    assert fake.last_ex == 99  # ttl threaded through to the client
    # default ttl when none passed
    wt.set_session("sid", "tokens", "1234")
    assert fake.last_ex == wt.session_ttl


def test_del_session_drops_the_key() -> None:
    fake = FakeValkey()
    wt = _wired(fake)
    wt.set_session("sid", "summary", "hello")
    assert wt.get_session("sid", "summary") == "hello"
    assert wt.del_session("sid", "summary") is True   # MEM-12: /reset drops the warm summary
    assert wt.get_session("sid", "summary") is None
    assert wt.del_session("", "summary") is False     # no sid → no-op


def test_cache_roundtrip_and_key_isolation() -> None:
    wt = _wired(FakeValkey())
    assert wt.cache_set("what is the canary token", ["ZEBRA-7741"]) is True
    assert wt.cache_get("what is the canary token") == ["ZEBRA-7741"]
    assert wt.cache_get("a different query") is None  # distinct sha1 → miss
    assert wt.cache_get("") is None  # empty query → no-op


def test_fail_soft_on_a_dead_client() -> None:
    wt = _wired(DeadValkey())
    assert wt.is_available() is False              # ping raises → unavailable, no exception
    assert wt.get_session("s", "summary") is None
    assert wt.set_session("s", "summary", "x") is False
    assert wt.cache_get("q") is None
    assert wt.cache_set("q", ["a"]) is False
