"""S14-5 scope-aware forget + scope-metadata tagging.

Tests the cold memory forget endpoint, warm-tier exact-scope deletion,
lesson-provider forget delegation, and the gx10 orchestration fan-out.
"""
from __future__ import annotations

import sys
import threading
import time
import types
from pathlib import Path

import pytest

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=lambda **kw: object()))
_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import project_context as pc  # noqa: E402
from project_context import ProjectContext  # noqa: E402
import gx10  # noqa: E402
import memory  # noqa: E402
import warm  # noqa: E402
from ack import lessons as L  # noqa: E402


# ── helpers ─────────────────────────────────────────────────────────────────


# A fake redis for the warm tier — dict-backed, supports scan_iter(match=...) + delete(*keys).
import fnmatch


class FakeRedis:
    def __init__(self, keys):
        self.store = {k: "v" for k in keys}
        self.deleted = []

    def ping(self):
        return True

    def scan_iter(self, match=None, count=None):
        for k in list(self.store.keys()):
            if match is None or fnmatch.fnmatchcase(k, match):
                yield k

    def delete(self, *keys):
        n = 0
        for k in keys:
            if k in self.store:
                del self.store[k]
                self.deleted.append(k)
                n += 1
        return n


def _warm_with_fake(keys):
    """Build a WarmTier whose _conn() returns a FakeRedis without importing redis."""
    w = warm.WarmTier({"url": "redis://x", "enabled": True})
    w._client = FakeRedis(keys)
    w._tried = True
    return w


# ── memory.forget ───────────────────────────────────────────────────────────


def test_forget_posts_delete_all_with_scope(monkeypatch) -> None:
    mm = memory.MemoryManager({"base_url": "http://x", "enabled": True, "agent_id": "ironclad"})
    captured = []

    def fake_post(path, body, timeout):
        captured.append((path, body))
        return {}

    monkeypatch.setattr(mm, "_post", fake_post)
    assert mm.forget("ns123") is True
    assert captured == [("/delete_all", {"agent_id": "ns123"})]


def test_forget_empty_scope_is_failclosed_noop(monkeypatch) -> None:
    mm = memory.MemoryManager({"base_url": "http://x", "enabled": True, "agent_id": "ironclad"})
    captured = []

    def fake_post(path, body, timeout):
        captured.append((path, body))
        return {}

    monkeypatch.setattr(mm, "_post", fake_post)
    assert mm.forget("") is False
    assert mm.forget(" ") is False
    assert captured == []


def test_forget_failsoft_on_transport_error(monkeypatch) -> None:
    mm = memory.MemoryManager({"base_url": "http://x", "enabled": True, "agent_id": "ironclad"})

    def fake_post(path, body, timeout):
        raise RuntimeError("boom")

    monkeypatch.setattr(mm, "_post", fake_post)
    assert mm.forget("ns") is False


def test_forget_disabled_returns_false(monkeypatch) -> None:
    mm = memory.MemoryManager({"base_url": "", "enabled": False, "agent_id": "ironclad"})
    captured = []

    def fake_post(path, body, timeout):
        captured.append((path, body))
        return {}

    monkeypatch.setattr(mm, "_post", fake_post)
    assert mm.forget("ns") is False
    assert captured == []


# ── memory scope-tagging ────────────────────────────────────────────────────


def _capture_add_bulk_body(monkeypatch, mm):
    """Monkeypatch mm._post and mm.is_available, return a (captured, lock) pair."""
    captured = []
    lock = threading.Lock()

    def fake_post(path, body, timeout):
        with lock:
            captured.append(body)
        return {}

    monkeypatch.setattr(mm, "_post", fake_post)
    monkeypatch.setattr(mm, "is_available", lambda: True)
    return captured, lock


def _wait_for_capture(captured, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not captured:
        time.sleep(0.02)


def test_add_bulk_tags_scope_when_bound(monkeypatch) -> None:
    mm = memory.MemoryManager({"base_url": "http://x", "enabled": True, "agent_id": "ironclad"})
    captured, _ = _capture_add_bulk_body(monkeypatch, mm)

    with pc.use(ProjectContext("p", "/r", "ns")):
        mm.add_bulk("some text", {"k": "v"})

    _wait_for_capture(captured)
    assert captured, "fire-and-forget /add_bulk body was not captured"
    body = captured[0]
    assert body["metadata"]["scope"] == "ns"
    assert body["agent_id"] == "ns"


def test_add_bulk_no_scope_tag_without_ctx(monkeypatch) -> None:
    mm = memory.MemoryManager({"base_url": "http://x", "enabled": True, "agent_id": "ironclad"})
    captured, _ = _capture_add_bulk_body(monkeypatch, mm)

    assert pc.current() is None
    mm.add_bulk("some text", {"k": "v"})

    _wait_for_capture(captured)
    assert captured, "fire-and-forget /add_bulk body was not captured"
    body = captured[0]
    assert "scope" not in body["metadata"]
    assert body["agent_id"] == "ironclad"


def test_store_task_completion_tags_scope_when_bound(monkeypatch) -> None:
    mm = memory.MemoryManager({"base_url": "http://x", "enabled": True, "agent_id": "ironclad"})
    captured, _ = _capture_add_bulk_body(monkeypatch, mm)

    with pc.use(ProjectContext("p", "/r", "ns")):
        mm.store_task_completion("t1", {"type": "software", "title": "x"}, "done")

    _wait_for_capture(captured)
    assert captured, "fire-and-forget /add body was not captured"
    body = captured[0]
    assert body["metadata"]["scope"] == "ns"


def test_tag_scope_helper_is_byte_identical_without_ctx() -> None:
    mm = memory.MemoryManager({"base_url": "http://x", "enabled": True, "agent_id": "ironclad"})
    assert pc.current() is None
    assert mm._tag_scope({"a": 1}) == {"a": 1}

    with pc.use(ProjectContext("p", "/r", "ns")):
        assert mm._tag_scope({"a": 1}) == {"a": 1, "scope": "ns"}


# ── warm.forget_scope ───────────────────────────────────────────────────────


def test_forget_scope_deletes_exact_session_and_cache() -> None:
    w = _warm_with_fake(
        [
            "session:abc:summary",
            "session:abc:tokens",
            "ret:abc:deadbeef",
            "session:other:summary",
            "ret:other:cafe",
        ]
    )
    assert w.forget_scope("abc") == 3
    assert sorted(w._client.deleted) == sorted(
        ["session:abc:summary", "session:abc:tokens", "ret:abc:deadbeef"]
    )
    assert "session:other:summary" in w._client.store
    assert "ret:other:cafe" in w._client.store


def test_forget_scope_is_exact_does_not_cascade_to_tracks() -> None:
    w = _warm_with_fake(
        [
            "session:abc:summary",
            "ret:abc:deadbeef",
            "session:abc::track::x:summary",
            "ret:abc::track::x:cafe",
        ]
    )
    assert w.forget_scope("abc") == 2
    assert "session:abc:summary" not in w._client.store
    assert "ret:abc:deadbeef" not in w._client.store
    assert "session:abc::track::x:summary" in w._client.store
    assert "ret:abc::track::x:cafe" in w._client.store

    assert w.forget_scope("abc::track::x") == 2
    assert "session:abc::track::x:summary" not in w._client.store
    assert "ret:abc::track::x:cafe" not in w._client.store


def test_forget_scope_empty_is_noop() -> None:
    w = _warm_with_fake(["session:abc:summary", "ret:abc:deadbeef"])
    assert w.forget_scope("") == 0
    assert w._client.deleted == []


def test_forget_scope_rejects_glob_scope() -> None:
    w = _warm_with_fake(["session:abc:summary", "ret:abc:deadbeef"])
    for bad in ["a*", "a?", "a[x]", "a\\b"]:
        assert w.forget_scope(bad) == 0
    assert w._client.deleted == []


def test_forget_scope_unavailable_returns_zero() -> None:
    w = warm.WarmTier({"url": "redis://x", "enabled": True})
    w._client = None
    w._tried = True
    assert w.forget_scope("abc") == 0


# ── lessons.forget ──────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_provider():
    L.set_provider(None)
    yield
    L.set_provider(None)


class ProviderWithoutForget:
    def get_lessons(self, scope, query="", limit=10):
        return []

    def report_lesson(self, scope, lesson, metadata=None):
        pass

    def brief(self, scopes, limit=10):
        return ""


class ProviderWithForget:
    def __init__(self):
        self.seen = []

    def get_lessons(self, scope, query="", limit=10):
        return []

    def report_lesson(self, scope, lesson, metadata=None):
        pass

    def brief(self, scopes, limit=10):
        return ""

    def forget(self, scope):
        self.seen.append(scope)


class ProviderWithRaisingForget:
    def get_lessons(self, scope, query="", limit=10):
        return []

    def report_lesson(self, scope, lesson, metadata=None):
        pass

    def forget(self, scope):
        raise RuntimeError("boom")


def test_lessons_forget_no_provider_is_false() -> None:
    L.set_provider(None)
    assert L.forget("s") is False


def test_lessons_forget_provider_without_forget_is_false() -> None:
    L.set_provider(ProviderWithoutForget())
    assert L.forget("s") is False


def test_lessons_forget_delegates_and_records() -> None:
    p = ProviderWithForget()
    L.set_provider(p)
    assert L.forget("ns") is True
    assert p.seen == ["ns"]


def test_lessons_forget_failsoft_on_raise() -> None:
    L.set_provider(ProviderWithRaisingForget())
    assert L.forget("s") is False


def test_forget_in_all() -> None:
    assert "forget" in L.__all__


# ── gx10._forget_scope ──────────────────────────────────────────────────────


class ColdForget:
    def __init__(self):
        self.seen = []

    def forget(self, scope):
        self.seen.append(scope)
        return True


class WarmForget:
    def __init__(self):
        self.seen = []

    def forget_scope(self, scope):
        self.seen.append(scope)
        return 5


class ColdForgetRaise:
    def forget(self, scope):
        raise RuntimeError("cold boom")


class WarmForgetRaise:
    def forget_scope(self, scope):
        raise RuntimeError("warm boom")


def test_engine_forget_scope_empty_is_noop_no_calls(monkeypatch) -> None:
    cold = ColdForget()
    warm_tier = WarmForget()
    monkeypatch.setattr(gx10, "_MEMORY", cold)
    monkeypatch.setattr(gx10, "_WARM", warm_tier)
    assert gx10._forget_scope("") == {
        "scope": "",
        "cold": False,
        "warm": 0,
        "lessons": False,
    }
    assert cold.seen == []
    assert warm_tier.seen == []


def test_engine_forget_scope_fans_out(monkeypatch) -> None:
    cold = ColdForget()
    warm_tier = WarmForget()
    lesson_provider = ProviderWithForget()
    L.set_provider(lesson_provider)

    monkeypatch.setattr(gx10, "_MEMORY", cold)
    monkeypatch.setattr(gx10, "_WARM", warm_tier)

    assert gx10._forget_scope("ns") == {
        "scope": "ns",
        "cold": True,
        "warm": 5,
        "lessons": True,
    }
    assert cold.seen == ["ns"]
    assert warm_tier.seen == ["ns"]
    assert lesson_provider.seen == ["ns"]
    L.set_provider(None)


def test_engine_forget_scope_failsoft_each_leg(monkeypatch) -> None:
    L.set_provider(ProviderWithRaisingForget())
    monkeypatch.setattr(gx10, "_MEMORY", ColdForgetRaise())
    monkeypatch.setattr(gx10, "_WARM", WarmForgetRaise())

    assert gx10._forget_scope("ns") == {
        "scope": "ns",
        "cold": False,
        "warm": 0,
        "lessons": False,
    }
    L.set_provider(None)
