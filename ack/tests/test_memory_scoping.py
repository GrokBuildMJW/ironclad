"""S3b memory scoping: ProjectContext drives MemoryManager._ids() and gx10 warm session.

These tests live in ack/tests but exercise core/engine modules via sys.path.
"""
from __future__ import annotations

import sys
import threading
import time
import hashlib
from pathlib import Path

import pytest

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import project_context as pc  # noqa: E402
from project_context import ProjectContext  # noqa: E402
import memory  # noqa: E402
import gx10  # noqa: E402
import warm  # noqa: E402


def test_ids_defaults_to_instance_agent_id() -> None:
    mm = memory.MemoryManager({"base_url": "http://x", "agent_id": "ironclad"})
    assert pc.current() is None
    ids = mm._ids()
    assert ids["agent_id"] == "ironclad"


def test_ids_uses_ctx_mem_ns_when_active() -> None:
    mm = memory.MemoryManager({"base_url": "http://x", "agent_id": "ironclad"})
    ctx = ProjectContext("p", "/r", "deadbeefcafe1234")
    assert pc.current() is None
    with pc.use(ctx):
        assert mm._ids()["agent_id"] == "deadbeefcafe1234"
    assert pc.current() is None
    assert mm._ids()["agent_id"] == "ironclad"


def test_ids_includes_user_id_when_set() -> None:
    mm = memory.MemoryManager(
        {"base_url": "http://x", "agent_id": "ironclad", "user_id": "u1"}
    )
    ids = mm._ids()
    assert ids["user_id"] == "u1"
    assert ids["agent_id"] == "ironclad"


def test_write_snapshots_ctx_scope_into_thread(monkeypatch) -> None:
    mm = memory.MemoryManager(
        {"base_url": "http://x", "enabled": True, "agent_id": "ironclad"}
    )

    captured: list[dict] = []
    lock = threading.Lock()

    def fake_post(path: str, body: dict, timeout: float) -> dict:
        with lock:
            captured.append(body)
        return {}

    monkeypatch.setattr(mm, "_post", fake_post)

    ctx = ProjectContext("p", "/r", "feed1234beef5678")
    with pc.use(ctx):
        mm.store_task_completion(
            "t1", {"type": "software", "title": "x"}, "done"
        )

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and not captured:
        time.sleep(0.02)

    assert captured, "fire-and-forget /add body was not captured"
    assert captured[0]["agent_id"] == "feed1234beef5678"


def test_warm_session_defaults_to_main(monkeypatch) -> None:
    monkeypatch.setattr(gx10, "WARM_SESSION_ID", "main")
    assert pc.current() is None
    assert gx10._active_warm_session() == "main"


def test_add_bulk_snapshots_ctx_scope(monkeypatch) -> None:
    mm = memory.MemoryManager(
        {"base_url": "http://x", "enabled": True, "agent_id": "ironclad"}
    )

    captured: list[dict] = []
    lock = threading.Lock()

    def fake_post(path: str, body: dict, timeout: float) -> dict:
        with lock:
            captured.append(body)
        return {}

    monkeypatch.setattr(mm, "_post", fake_post)

    ctx = ProjectContext("p", "/r", "aaaa1111bbbb2222")
    with pc.use(ctx):
        mm.add_bulk("text", {"k": "v"})

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and not captured:
        time.sleep(0.02)

    assert captured, "fire-and-forget /add_bulk body was not captured"
    assert captured[0]["agent_id"] == "aaaa1111bbbb2222"


def test_chunk_and_store_snapshots_ctx_scope(monkeypatch) -> None:
    mm = memory.MemoryManager(
        {"base_url": "http://x", "enabled": True, "agent_id": "ironclad"}
    )

    captured: list[dict] = []
    lock = threading.Lock()

    def fake_post(path: str, body: dict, timeout: float) -> dict:
        with lock:
            captured.append(body)
        return {}

    monkeypatch.setattr(mm, "_post", fake_post)

    ctx = ProjectContext("p", "/r", "aaaa1111bbbb2222")
    with pc.use(ctx):
        mm.chunk_and_store("x" * 20000, {"k": "v"})

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and not captured:
        time.sleep(0.02)

    assert captured, "no /add_bulk chunk bodies were captured"
    assert all(body["agent_id"] == "aaaa1111bbbb2222" for body in captured)


def test_active_mem_ns_resolves() -> None:
    assert pc.current() is None
    assert gx10._active_mem_ns() == ""
    assert gx10._active_mem_ns(default="ironclad") == "ironclad"

    ctx = ProjectContext("p", "/r", "ccccdddd0000ffff")
    with pc.use(ctx):
        assert gx10._active_mem_ns() == "ccccdddd0000ffff"
        assert gx10._active_mem_ns(default="ironclad") == "ccccdddd0000ffff"

    assert pc.current() is None


def test_warm_cache_key_is_namespaced() -> None:
    key_a = warm.WarmTier._key("q", "nsA")
    key_b = warm.WarmTier._key("q", "nsB")
    key_default = warm.WarmTier._key("q", "")

    assert key_a != key_b
    assert key_b != key_default
    assert key_a != key_default
    assert warm.WarmTier._key("q", "nsA") == key_a


def test_retrieve_hits_passes_ctx_namespace_to_cache(monkeypatch) -> None:
    class FakeMem:
        def is_available(self) -> bool:
            return True

        def search(self, query: str, top_k: int) -> list[str]:
            return ["cold hit"]

    class FakeWarm:
        def __init__(self) -> None:
            self.get_namespaces: list[str] = []
            self.set_namespaces: list[str] = []

        def is_available(self) -> bool:
            return True

        def cache_get(self, query: str, namespace: str = "") -> None:
            self.get_namespaces.append(namespace)
            return None

        def cache_set(
            self,
            query: str,
            results: list[str],
            namespace: str = "",
            ttl: int | None = None,
        ) -> bool:
            self.set_namespaces.append(namespace)
            return True

    monkeypatch.setattr(gx10, "_MEMORY", FakeMem())
    monkeypatch.setattr(gx10, "_WARM", FakeWarm())

    ctx = ProjectContext("p", "/r", "eeee9999aaaa8888")
    with pc.use(ctx):
        hits = gx10._retrieve_hits("q", 5)

    assert hits == ["cold hit"]
    warm_obj = gx10._WARM
    assert isinstance(warm_obj, FakeWarm)
    assert warm_obj.get_namespaces == ["eeee9999aaaa8888"]
    assert warm_obj.set_namespaces == ["eeee9999aaaa8888"]



def test_warm_session_scopes_by_ctx(monkeypatch) -> None:
    monkeypatch.setattr(gx10, "WARM_SESSION_ID", "main")
    ctx = ProjectContext("p", "/r", "aaaabbbbccccdddd")
    assert pc.current() is None
    assert gx10._active_warm_session() == "main"
    with pc.use(ctx):
        assert gx10._active_warm_session() == "aaaabbbbccccdddd"
    assert pc.current() is None
    assert gx10._active_warm_session() == "main"


def test_warm_cache_empty_namespace_preserves_legacy_key():
    """No active project (empty namespace) must keep the LEGACY ret:<sha1> warm key byte-identical
    (no-ctx behaviour unchanged); a set namespace produces ret:<ns>:<sha1>."""
    h = hashlib.sha1(b"hello").hexdigest()
    assert warm.WarmTier._key("hello", "") == "ret:" + h            # legacy, byte-identical to main
    assert warm.WarmTier._key("hello", "nsX") == "ret:nsX:" + h     # namespaced
    assert warm.WarmTier._key("hello") == "ret:" + h               # default namespace == legacy
