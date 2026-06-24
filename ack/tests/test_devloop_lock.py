"""Machine-gated dev-loop single-driver sentinel (epic #312 S4 / ADR-0002 D7), offline.

Lives in `scripts/devloop/` (private) -> skips in an installed/clean-room tree. Pins the lock contract:
a second live driver is refused (LockHeld), release frees it, a STALE lock (dead owner pid) is reclaimed,
and release is idempotent. Cross-platform (atomic O_EXCL create).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_LOCK = _REPO / "scripts" / "devloop" / "lock.py"

pytestmark = pytest.mark.skipif(
    not _LOCK.is_file(),
    reason="private dev-loop lock (scripts/devloop/lock.py) absent — clean-room tree",
)


def _load():
    spec = importlib.util.spec_from_file_location("_devloop_lock", _LOCK)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_second_live_driver_is_refused(tmp_path):
    lk = _load()
    p = tmp_path / ".devloop" / "driver.lock"
    h = lk.acquire(p)                                   # first driver holds it (our own, live, pid)
    with pytest.raises(lk.LockHeld):
        lk.acquire(p)                                  # a second driver is refused
    lk.release(h)
    h2 = lk.acquire(p)                                 # released -> a new driver may claim it
    lk.release(h2)


def test_stale_lock_is_reclaimed(tmp_path):
    lk = _load()
    p = tmp_path / ".devloop" / "driver.lock"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("0", encoding="utf-8")                # an owner pid of 0 is never alive => stale
    h = lk.acquire(p)                                  # reclaimed, not wedged
    assert Path(p).read_text(encoding="utf-8").strip() == str(__import__("os").getpid())
    lk.release(h)
    assert not p.exists()


def test_release_is_idempotent(tmp_path):
    lk = _load()
    p = tmp_path / ".devloop" / "driver.lock"
    h = lk.acquire(p)
    lk.release(h)
    lk.release(h)                                       # second release is a no-op, never raises
