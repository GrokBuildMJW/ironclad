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


def test_dead_client_clears_the_tried_latch_so_conn_retries() -> None:
    # WARM-1 (#503): a transient Valkey blip drops the client; the _tried latch MUST clear too, else
    # _conn() short-circuits on _tried and returns None forever (the documented retry is impossible).
    wt = _wired(DeadValkey())
    assert wt._tried is True                       # _wired pre-sets the latch (lazy connect bypassed)
    assert wt.is_available() is False              # ping raises → unavailable
    assert wt._client is None and wt._tried is False   # dead client dropped AND latch cleared → _conn retries


def test_public_valkey_default_is_loopback_no_auth() -> None:
    # #488: the PUBLIC docker-compose.yml valkey service MUST keep the safe default — loopback-only bind and
    # NO requirepass. A LAN bind + auth is layered via a deploy-local compose override kept OUTSIDE the synced
    # tree (so a re-deploy can't wipe it); this guards that the shipped public file never accidentally ships a
    # LAN-bound or unauthenticated-on-LAN Valkey, which was the #488 / #385 drift risk.
    compose = Path(__file__).resolve().parents[2] / "docker-compose.yml"
    if not compose.is_file():
        import pytest
        pytest.skip("docker-compose.yml not present in this tree")
    text = compose.read_text(encoding="utf-8")
    # exclude comment lines: a comment may legitimately *describe* the deploy-local LAN/--requirepass pattern;
    # the guard is about the actual shipped config (port mappings + the valkey-server command), not prose.
    config_lines = [ln for ln in text.splitlines() if not ln.strip().startswith("#")]
    port_lines = [ln.strip() for ln in config_lines if "6379:6379" in ln and ln.strip().startswith("-")]
    assert port_lines, "valkey 6379 port mapping not found in the public compose"
    for ln in port_lines:
        assert "127.0.0.1:6379:6379" in ln, f"public valkey must bind loopback only, got: {ln}"
        assert "0.0.0.0" not in ln, f"public valkey must not bind all interfaces, got: {ln}"
    assert not any("--requirepass" in ln for ln in config_lines), \
        "the public compose must not hardcode a Valkey requirepass (auth is deploy-local, #488)"
