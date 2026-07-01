"""Pytest bootstrap for the ACK test suite.

Puts ``core/`` on ``sys.path`` so the tests can ``import ack`` (and ``ack.lodestar``)
regardless of the invocation directory.
"""
import sys
from pathlib import Path

import pytest

# ack/tests/conftest.py → parents[2] == core/
CORE_DIR = Path(__file__).resolve().parents[2]
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))


@pytest.fixture(autouse=True)
def _ace_isolation(request, tmp_path, monkeypatch):
    """Isolate ACE's process-global state (epic #855 / #863). ACE is ALWAYS-ON: every ``gx10._apply_config``
    registers a process-global PlaybookStore provider, starts a background ReflectionWorker daemon, and may
    migrate the legacy lesson tree under ``ironclad_home()``. Left unmanaged, that state (the daemon thread +
    the registered provider) would bleed across the suite and pollute later tests. So, for every ACK test:

      * point ``ironclad_home`` at the test's tmp dir (no test ever touches the real install home) — except
        ``test_project_registry``, which tests ``ironclad_home`` itself;
      * after the test, stop the worker daemon + clear the global store/provider so the next test starts clean.

    Only resets modules that are already imported (no fresh import → no openai/select shadow surprises)."""
    pr = sys.modules.get("project_registry")
    if pr is not None and request.module.__name__ != "test_project_registry":
        monkeypatch.setattr(pr, "ironclad_home", lambda: tmp_path, raising=False)
    yield
    gx10 = sys.modules.get("gx10")
    if gx10 is None:
        return
    for wattr in ("_ACE_WORKER", "_ACE_FORK_WORKER"):     # M5-2: also stop the fork MPR worker
        worker = getattr(gx10, wattr, None)
        if worker is not None:
            try:
                worker.stop()
            except Exception:
                pass
    for attr, val in (("_ACE_WORKER", None), ("_ACE_STORE", None), ("_ACE_MIGRATED", False),
                      ("_ACE_FORK_WORKER", None), ("_ACE_FORK_MPR", False)):
        if hasattr(gx10, attr):
            setattr(gx10, attr, val)
    if hasattr(gx10, "_ACE_FORK_INFLIGHT"):
        try:
            gx10._ACE_FORK_INFLIGHT.clear()
        except Exception:
            pass
    lessons = sys.modules.get("ack.lessons")
    pbs = sys.modules.get("playbook_store")
    if lessons is not None and pbs is not None:
        try:
            if isinstance(lessons.get_provider(), pbs.PlaybookStore):
                lessons.set_provider(None)
        except Exception:
            pass
