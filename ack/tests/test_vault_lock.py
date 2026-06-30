from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=lambda **kw: object()))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import project_context as pc
from project_context import ProjectContext
import project_registry as pr
import gx10


def test_vault_lock_path_per_project_and_track(tmp_path):
    reg = pr.Registry(home=tmp_path)
    a = reg.vault_lock("p", "main").path
    b = reg.vault_lock("p", "feature").path
    c = reg.vault_lock("q", "main").path
    assert a != b and a != c and b != c
    assert (tmp_path / "locks" / "vault") in a.parents


def test_vault_lock_default_track_is_main(tmp_path):
    reg = pr.Registry(home=tmp_path)
    assert reg.vault_lock("p").path == reg.vault_lock("p", "main").path


def test_vault_lock_distinct_from_project_lock(tmp_path):
    reg = pr.Registry(home=tmp_path)
    assert reg.vault_lock("p").path != reg.project_lock("p").path


def test_vault_lock_rejects_unsafe_ids(tmp_path):
    reg = pr.Registry(home=tmp_path)
    for bad in ("..", ".", "a/b", "a\\b", ""):
        with pytest.raises(ValueError):
            reg.vault_lock(bad, "main")
        with pytest.raises(ValueError):
            reg.vault_lock("p", bad)


def test_vault_lock_serializes_same_key(tmp_path):
    reg = pr.Registry(home=tmp_path)
    lk1 = reg.vault_lock("p", "main")
    lk1.acquire()
    try:
        lk2 = reg.vault_lock("p", "main", timeout_s=0.0)
        raised = False
        try:
            lk2.acquire()
            lk2.release()
        except Exception:
            raised = True
        assert raised
    finally:
        lk1.release()


def test_vault_lock_different_tracks_do_not_block(tmp_path):
    reg = pr.Registry(home=tmp_path)
    lk1 = reg.vault_lock("p", "main")
    lk1.acquire()
    try:
        lk2 = reg.vault_lock("p", "other", timeout_s=0.0)
        lk2.acquire()
        lk2.release()  # different track key → no contention
    finally:
        lk1.release()


def test_gx10_vault_lock_reentrant(monkeypatch, tmp_path):
    monkeypatch.setattr(gx10, "_REGISTRY", pr.Registry(home=tmp_path))
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        with gx10._vault_lock():
            with gx10._vault_lock():
                pass  # nested must not deadlock; reaching here passes
    assert True


def test_gx10_vault_lock_releases(monkeypatch, tmp_path):
    reg = pr.Registry(home=tmp_path)
    monkeypatch.setattr(gx10, "_REGISTRY", reg)
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        with gx10._vault_lock():
            pass  # after the block the lock must be free
    # a fresh timeout=0 acquire succeeds
    lk = reg.vault_lock("p", "main", timeout_s=0.0)
    lk.acquire()
    lk.release()


def test_gx10_vault_lock_fail_soft_without_registry(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(gx10, "_REGISTRY", None)
    assert pc.current() is None
    with gx10._vault_lock():
        pass  # no registry / no ctx → still works (best-effort, no raise)
    assert True


def test_resolve_vault_lock_uses_registry_when_active(monkeypatch, tmp_path):
    reg = pr.Registry(home=tmp_path)
    monkeypatch.setattr(gx10, "_REGISTRY", reg)
    with pc.use(ProjectContext("p", str(tmp_path), "ns", track="feature")):
        lk = gx10._resolve_vault_lock()
        assert lk is not None
        assert lk.path == reg.vault_lock("p", "feature").path


class _SpyLock:
    def __init__(self):
        self.acquired = 0
        self.released = 0

    def acquire(self):
        self.acquired += 1
        return self

    def release(self):
        self.released += 1


def test_vault_lock_releases_on_exception(monkeypatch, tmp_path):
    reg = pr.Registry(home=tmp_path)
    monkeypatch.setattr(gx10, "_REGISTRY", reg)
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        with pytest.raises(RuntimeError):
            with gx10._vault_lock():
                raise RuntimeError("boom")
        # the OS lock is released and the reentrancy depth reset → a fresh acquire succeeds
        lk = reg.vault_lock("p", "main", timeout_s=0.0)
        lk.acquire()
        lk.release()
        with gx10._vault_lock():
            pass


def test_stage_handover_runs_under_vault_lock(monkeypatch, tmp_path):
    spy = _SpyLock()
    monkeypatch.setattr(gx10, "_resolve_vault_lock", lambda: spy)
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        out = gx10._stage_handover(None, "NOPE-AGENT", "handover body")
    assert spy.acquired == 1 and spy.released == 1   # even the early-return path runs under the lock
    assert "unknown agent" in out.lower()


def test_advance_pipeline_runs_under_vault_lock(monkeypatch, tmp_path):
    spy = _SpyLock()
    monkeypatch.setattr(gx10, "_resolve_vault_lock", lambda: spy)
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        try:
            gx10._advance_pipeline("T999", "NOPE-AGENT")
        except Exception:
            pass   # business outcome irrelevant; the lock must be taken + released either way
    assert spy.acquired == 1 and spy.released == 1
