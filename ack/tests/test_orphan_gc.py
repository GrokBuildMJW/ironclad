from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=lambda **kw: object()))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10
import memory
import project_registry as pr

A = "aaaa1111bbbb2222"
B = "bbbb2222cccc3333"
C = "cccc3333dddd4444"


def test_list_scopes_returns_string_list(monkeypatch):
    """list_scopes returns the scopes returned by the backend verbatim."""
    mm = memory.MemoryManager({"base_url": "http://x", "enabled": True, "agent_id": "ironclad"})
    monkeypatch.setattr(mm, "_get", lambda _path, _timeout: {"scopes": [A, B, "ironclad"]})
    assert mm.list_scopes() == [A, B, "ironclad"]


def test_list_scopes_filters_garbage(monkeypatch):
    """Non-string and empty entries are dropped from the scope list."""
    mm = memory.MemoryManager({"base_url": "http://x", "enabled": True, "agent_id": "ironclad"})
    monkeypatch.setattr(mm, "_get", lambda _path, _timeout: {"scopes": [A, 123, None, "", "ok"]})
    assert mm.list_scopes() == [A, "ok"]


def test_list_scopes_non_dict_or_missing_is_empty(monkeypatch):
    """A non-dict response or a dict without 'scopes' yields an empty list."""
    mm = memory.MemoryManager({"base_url": "http://x", "enabled": True, "agent_id": "ironclad"})
    monkeypatch.setattr(mm, "_get", lambda _path, _timeout: ["not", "a", "dict"])
    assert mm.list_scopes() == []
    monkeypatch.setattr(mm, "_get", lambda _path, _timeout: {"other": 1})
    assert mm.list_scopes() == []


def test_list_scopes_failsoft_on_error(monkeypatch):
    """Transport or backend errors degrade to an empty list."""
    mm = memory.MemoryManager({"base_url": "http://x", "enabled": True, "agent_id": "ironclad"})

    def _boom(_path, _timeout):
        raise RuntimeError("down")

    monkeypatch.setattr(mm, "_get", _boom)
    assert mm.list_scopes() == []


def test_list_scopes_disabled_is_empty(monkeypatch):
    """A disabled manager returns [] without ever calling the backend."""
    mm = memory.MemoryManager({"base_url": "", "enabled": True, "agent_id": "ironclad"})

    def _never_called(_path, _timeout):
        raise AssertionError("_get must not be called when disabled")

    monkeypatch.setattr(mm, "_get", _never_called)
    assert mm.list_scopes() == []


def test_orphan_minted_unregistered_is_orphan():
    """A minted mem_ns present but not registered is flagged as orphan."""
    assert pr.valid_mem_ns(A) is True
    assert pr.valid_mem_ns(B) is True
    assert gx10._orphan_scopes([A, B], [A]) == [B]


def test_orphan_registered_minted_is_kept():
    """A registered minted mem_ns is never an orphan."""
    assert gx10._orphan_scopes([A], [A]) == []


def test_orphan_base_and_human_named_never_flagged():
    """Base and human-named scopes are not minted mem_ns values, so never orphans."""
    assert gx10._orphan_scopes(["ironclad", "curated-global", "", None, A], []) == [A]


def test_orphan_track_subscope_of_registered_kept():
    """A track sub-scope is kept when its parent mem_ns is registered."""
    assert gx10._orphan_scopes([A + "::track::feat"], [A]) == []


def test_orphan_track_subscope_of_unregistered_flagged():
    """A track sub-scope is orphaned when its parent mem_ns is not registered."""
    assert gx10._orphan_scopes([B + "::track::x"], [A]) == [B + "::track::x"]


class FakeMem:
    def __init__(self, scopes):
        self._scopes = scopes

    def list_scopes(self):
        return list(self._scopes)


class FakeReg:
    def __init__(self, mem_ns_list):
        self._p = [types.SimpleNamespace(mem_ns=m) for m in mem_ns_list]

    def list(self):
        return list(self._p)


def test_reconcile_dry_run_lists_orphans_without_forgetting(monkeypatch):
    """dry_run=True reports orphans but does not call _forget_scope."""
    monkeypatch.setattr(gx10, "_MEMORY", FakeMem([A, B, "ironclad"]))
    monkeypatch.setattr(gx10, "_REGISTRY", FakeReg([A]))

    calls = []

    def spy(scope):
        calls.append(scope)
        return {"ok": True}

    monkeypatch.setattr(gx10, "_forget_scope", spy)

    out = gx10._reconcile_orphan_memory(dry_run=True)
    assert out["orphans"] == [B]
    assert out["forgotten"] == []
    assert out["dry_run"] is True
    assert calls == []


def test_reconcile_apply_forgets_orphans(monkeypatch):
    """dry_run=False forgets every orphan and reports what was forgotten."""
    monkeypatch.setattr(gx10, "_MEMORY", FakeMem([A, B, "ironclad"]))
    monkeypatch.setattr(gx10, "_REGISTRY", FakeReg([A]))

    calls = []

    def spy(scope):
        calls.append(scope)
        return {"ok": True}

    monkeypatch.setattr(gx10, "_forget_scope", spy)

    out = gx10._reconcile_orphan_memory(dry_run=False)
    assert out["orphans"] == [B]
    assert out["forgotten"] == [B]
    assert calls == [B]


def test_reconcile_no_memory_or_registry_is_empty(monkeypatch):
    """Without memory or registry the reconciler returns an empty report."""
    monkeypatch.setattr(gx10, "_MEMORY", None)
    monkeypatch.setattr(gx10, "_REGISTRY", FakeReg([A]))
    out = gx10._reconcile_orphan_memory()
    assert out == {"present": [], "registered": [], "orphans": [], "forgotten": [], "dry_run": True}

    monkeypatch.setattr(gx10, "_MEMORY", FakeMem([A, B]))
    monkeypatch.setattr(gx10, "_REGISTRY", None)
    out = gx10._reconcile_orphan_memory()
    assert out == {"present": [], "registered": [], "orphans": [], "forgotten": [], "dry_run": True}


def test_reconcile_failsoft_when_one_forget_raises(monkeypatch):
    """A failure to forget one orphan is swallowed and remaining orphans are still processed."""
    monkeypatch.setattr(gx10, "_MEMORY", FakeMem([A, B, C]))
    monkeypatch.setattr(gx10, "_REGISTRY", FakeReg([A]))

    calls = []

    def spy(scope):
        calls.append(scope)
        if scope == B:
            raise RuntimeError("forget failed")
        return {"ok": True}

    monkeypatch.setattr(gx10, "_forget_scope", spy)

    out = gx10._reconcile_orphan_memory(dry_run=False)
    assert out["orphans"] == [B, C]
    assert out["forgotten"] == [C]
    assert calls == [B, C]


class RaisingReg:
    def list(self):
        raise RuntimeError("registry unreadable")


def test_reconcile_failclosed_when_registry_list_raises(monkeypatch):
    """If the registry can't be enumerated, the GC must REFUSE — never treat every present minted scope as
    an orphan and delete it (the registered set would be empty)."""
    monkeypatch.setattr(gx10, "_MEMORY", FakeMem([A, B, C]))
    monkeypatch.setattr(gx10, "_REGISTRY", RaisingReg())

    calls = []
    monkeypatch.setattr(gx10, "_forget_scope", lambda scope: calls.append(scope))

    out = gx10._reconcile_orphan_memory(dry_run=False)
    assert out["orphans"] == []        # nothing flagged when we cannot know what is registered
    assert out["forgotten"] == []
    assert calls == []                 # and crucially: NOTHING was deleted
    assert out["present"] == [A, B, C]  # present is still reported (it read fine)


def test_base_agent_id_normalized(monkeypatch):
    """A blank / whitespace / non-string base agent_id falls back to 'ironclad' (so it is never an unscoped
    request the service would reject)."""
    assert memory.MemoryManager({"base_url": "http://x"}).agent_id == "ironclad"
    assert memory.MemoryManager({"base_url": "http://x", "agent_id": "   "}).agent_id == "ironclad"
    assert memory.MemoryManager({"base_url": "http://x", "agent_id": 123}).agent_id == "ironclad"
    assert memory.MemoryManager({"base_url": "http://x", "agent_id": " p1 "}).agent_id == "p1"
